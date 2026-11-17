"""Event-calendar executor for repeated heterogeneous inspection.

Wind is an externally supplied replay; estimates use the current whole field.
Units: seconds, metres, watts and watt-hours. Dependencies: Python stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from heapq import heappush, heappop
from itertools import count
from math import hypot, isfinite
from bisect import bisect_left
from copy import deepcopy

from .physics import EPS, ground_speed, zone_at, split_leg, overdue_integral
from .weather import Weather

AIR = {'takeoff', 'transit', 'service', 'service_ready', 'decision', 'return',
       'landing_wait', 'landing', 'arrived'}
MOVING = {'transit', 'return'}


@dataclass
class Aircraft:
    id: str
    profile: dict
    home: str
    base: str | None
    pos: tuple
    node: str
    energy: float
    phase: str = 'idle'
    stamp: int = 0
    due: float | None = None
    power: float = 0.0
    charge_battery: float = 0.0
    charge_grid: float = 0.0
    task: str | None = None
    recovery: str | None = None
    destination: str | None = None
    segment_end: tuple | None = None
    velocity: tuple = (0.0, 0.0)
    visited: set = field(default_factory=set)
    failed: str | None = None
    sortie: int = 0
    sortie_origin: str | None = None
    queue_since: float | None = None
    hold_deadline: float | None = None
    safety_recorded: bool = False


@dataclass
class Task:
    id: str
    node: str
    template: str
    tau: float
    last: float
    priority: float
    release: float
    deadline: float
    active: bool = False
    owner: str | None = None
    step: int = 0
    progress: float = 0.0
    generation: int = 0
    aoi_area: float = 0.0
    overdue_area: float = 0.0
    updates: int = 0


def _number(value, label, minimum=0, strict=False):
    if not isinstance(value, (int, float)) or not isfinite(value) or (value <= minimum if strict else value < minimum):
        raise ValueError(f'Invalid {label}: {value}')
    return float(value)


class Simulation:
    def __init__(self, scenario, config=None, weather=None):
        self.s = deepcopy(scenario)
        self.cfg = dict(policy='B2', recovery='NEAR', max_targets_per_sortie=2,
                        execution_mode='full', air_capacity=24, record_events=True)
        self.cfg.update(config or {})
        if self.cfg['policy'] not in {'B0', 'B2'} or self.cfg['recovery'] not in {'HOME', 'NEAR', 'QUEUE'}:
            raise ValueError('Unknown task/recovery rule')
        if self.cfg['execution_mode'] not in {'full', 'accumulating', 'instant_charge'}:
            raise ValueError('Unknown execution mode')
        for key in ('max_targets_per_sortie', 'air_capacity'):
            if type(self.cfg[key]) is not int or self.cfg[key] < 1:
                raise ValueError(f'{key} must be a positive integer')
        clock = scenario['clock']
        self.a = _number(clock['evaluation_start_s'], 'evaluation_start_s')
        self.b = _number(clock['evaluation_end_s'], 'evaluation_end_s', self.a, True)
        self.end = _number(clock['recovery_end_s'], 'recovery_end_s', self.b)
        self.positions = {key: (float(v['x_m']), float(v['y_m'])) for key,v in scenario['positions'].items()}
        self.centers = {v['id']: (float(v['x_m']), float(v['y_m'])) for v in scenario['weather_zones']}
        if not self.positions or not self.centers:
            raise ValueError('Positions and weather zones cannot be empty')
        if any(not all(isfinite(x) for x in point) for point in list(self.positions.values())+list(self.centers.values())):
            raise ValueError('Non-finite spatial coordinate')
        if len(self.centers)!=len(scenario['weather_zones']) or len(set(self.centers.values()))!=len(self.centers):
            raise ValueError('Duplicate weather zone IDs or representative coordinates')
        self.weather = Weather(weather or scenario['weather'], self.centers, self.end)
        self.bases = {v['id']: dict(v) for v in scenario['bases']}
        if not self.bases or len(self.bases)!=len(scenario['bases']):
            raise ValueError('Empty/duplicate bases')
        self.templates = scenario['templates']
        self.business = scenario.get('business', 'periodic')
        if self.business not in {'periodic', 'batch'}:
            raise ValueError('Unknown business mode')
        self.allowed = None if scenario.get('allowed_legs') is None else {tuple(v) for v in scenario['allowed_legs']}
        self.adjacency = None
        if self.allowed is not None:
            self.adjacency = {key: [] for key in self.positions}
            for source, destination in sorted(self.allowed):
                self.adjacency[source].append(destination)
        self.fleet = {}
        for profile in scenario['profiles'].values():
            for key in ('battery_usable_wh','max_airspeed_mps','max_groundspeed_mps',
                        'min_groundspeed_mps','max_flight_wind_mps','takeoff_w','cruise_w',
                        'inspection_base_w','hold_w','landing_w','charge_intake_battery_w'):
                _number(profile[key],key,strict=True)
            for key in ('return_reserve_wh','ground_w'):_number(profile[key],key)
            if profile['min_groundspeed_mps']>profile['max_groundspeed_mps']:
                raise ValueError('Minimum ground speed exceeds maximum')
        for member in sorted(scenario['fleet'], key=lambda m:m['id']):
            profile = scenario['profiles'][member['profile_id']]
            home = member['home_base_id']
            node = self.bases[home]['position_id']
            battery = _number(profile['battery_usable_wh'], 'battery', strict=True)
            if profile['return_reserve_wh'] >= battery:
                raise ValueError('Reserve must be less than battery')
            soc = _number(member['initial_soc'], 'initial_soc')
            if soc > 1:
                raise ValueError('initial_soc exceeds 1')
            self.fleet[member['id']] = Aircraft(member['id'], profile, home, home,
                                               self.positions[node], node, battery*soc)
        if not self.fleet:
            raise ValueError('Fleet cannot be empty')
        self.tasks = {}
        for spec in scenario['tasks']:
            tau = _number(spec.get('interval_s', 1), 'interval', strict=True)
            age = _number(spec.get('initial_age_s', 0), 'initial_age')
            release = spec.get('release_s', max(0, .5*tau-age))
            deadline = spec.get('deadline_s', tau-age)
            if spec['template_id'] not in self.templates or spec['position_id'] not in self.positions:
                raise ValueError('Unknown task template/position')
            self.tasks[spec['id']] = Task(spec['id'], spec['position_id'], spec['template_id'],
                tau, -age, float(spec.get('priority_weight', 1)), release, deadline)
        if not self.tasks:
            raise ValueError('Tasks cannot be empty')
        if len(self.tasks) != len(scenario['tasks']) or len(self.fleet) != len(scenario['fleet']):
            raise ValueError('Duplicate task/aircraft IDs')
        for steps in self.templates.values():
            if not steps:
                raise ValueError('Empty template')
            for step in steps:
                _number(step['duration_s'], 'duration', strict=True)
                _number(step['wind_limit_mps'], 'service wind limit')
                _number(step.get('power_factor', 1), 'power factor', strict=True)
        for base in self.bases.values():
            if base['position_id'] not in self.positions:
                raise ValueError('Unknown base position')
            for key in ('pad_capacity', 'charge_ports'):
                if type(base[key]) is not int or base[key] < 1:
                    raise ValueError(f'{key} must be a positive integer')
            for key in ('port_input_w','takeoff_s','landing_s','turnaround_s'):
                _number(base[key],key,strict=True)
            _number(base['max_landing_wait_s'],'max_landing_wait_s')
            for key in ('charge_efficiency','target_soc'):
                if not 0 < base[key] <= 1:
                    raise ValueError(f'{key} must be in (0,1]')
        self.t = 0.0
        self.events, self.completions = [], []
        self.calendar, self.sequence = [], count()
        self.pad = {b:set() for b in self.bases}
        self.charge = {b:set() for b in self.bases}
        self.charge_queue = {b:[] for b in self.bases}
        self.land_queue = {b:[] for b in self.bases}
        self.air = set()
        self.max_occupancy = {'air':0,'pad':{b:0 for b in self.bases},'charge':{b:0 for b in self.bases}}
        self.initial_energy = sum(u.energy for u in self.fleet.values())
        self.charged = self.grid = self.consumed = self.eval_energy = self.tail_energy = 0.0
        self.per_energy = {k:dict(initial_wh=u.energy, consumed_wh=0.0, charged_battery_wh=0.0,grid_input_wh=0.0)
                           for k,u in self.fleet.items()}
        self.stats = dict(sorties=0, cross_base_recoveries=0, charging_wait_s=0.,landing_wait_s=0.,
                          service_interruptions=0, discarded_progress_s=0.,steps_completed=0,
                          candidate_scans=0, decisions=0, safety_violations=0)
        self.closed = False
        self.route_cache = {}
        self._running = False

    def emit(self, kind, u=None, **fields):
        event = dict(time_s=self.t, type=kind, **fields)
        if u is not None:
            event['aircraft_id'] = u.id
        if self.cfg['record_events']:
            self.events.append(event)

    def schedule(self, when, priority, kind, payload=None):
        if not isfinite(when) or when < self.t-EPS:
            raise ValueError(f'Invalid event time {when}, now {self.t}')
        if when <= self.end+EPS:
            heappush(self.calendar,(max(when,self.t),priority,next(self.sequence),kind,payload))

    def set_phase(self,u,phase,duration=None,power=None):
        u.stamp += 1
        u.phase = phase
        u.due = None if duration is None else self.t+max(0.,duration)
        u.power = self.power(u,phase) if power is None else power
        u.charge_battery = u.charge_grid = 0.0
        if phase=='charging':
            base=self.bases[u.base]
            u.charge_battery=min(base['charge_efficiency']*base['port_input_w'],u.profile['charge_intake_battery_w'])
            u.charge_grid=u.charge_battery/base['charge_efficiency']
        self.emit('phase_started',u,phase=phase,position=list(u.pos),energy_wh=u.energy,
                  base_id=u.base,destination=u.destination,power_w=u.power,
                  charge_battery_w=u.charge_battery,charge_grid_w=u.charge_grid,
                  task_id=u.task,step_index=self.tasks[u.task].step if u.task else None)
        if duration is not None:
            self.schedule(u.due,0,'finish',(u.id,u.stamp))
        if u.power>0:
            empty_at=self.t+u.energy/u.power*3600
            # A stage ending exactly at the threshold is completed first.
            if u.due is None or empty_at<u.due-EPS:
                self.schedule(empty_at,1,'empty',(u.id,u.stamp))
            if phase in AIR:
                reserve=u.profile['return_reserve_wh']
                crossing=self.t+max(0.,u.energy-reserve)/u.power*3600
                if (u.energy>reserve+EPS or not u.safety_recorded) and (u.due is None or crossing<u.due-EPS):
                    self.schedule(crossing,1,'reserve',(u.id,u.stamp))

    def power(self,u,phase):
        p=u.profile
        if phase=='takeoff':return p['takeoff_w']
        if phase in MOVING:return p['cruise_w']
        if phase=='landing':return p['landing_w']
        if phase=='landing_wait':return p['hold_w']
        if phase=='service':
            step=self.templates[self.tasks[u.task].template][self.tasks[u.task].step]
            return p['inspection_base_w']*step.get('power_factor',1)
        if phase in {'failed','charging'}:return 0.0
        return p['ground_w']

    def acquire(self,u,resource,base=None):
        holders=self.air if resource=='air' else (self.pad if resource=='pad' else self.charge)[base]
        cap=self.cfg['air_capacity'] if resource=='air' else self.bases[base]['pad_capacity' if resource=='pad' else 'charge_ports']
        if u.id in holders or len(holders)>=cap:raise RuntimeError('Resource capacity violation')
        holders.add(u.id)
        if resource=='air':self.max_occupancy['air']=max(self.max_occupancy['air'],len(holders))
        else:self.max_occupancy[resource][base]=max(self.max_occupancy[resource][base],len(holders))
        self.emit('resource_acquired',u,resource=resource,base_id=base)

    def release(self,u,resource,base=None):
        holders=self.air if resource=='air' else (self.pad if resource=='pad' else self.charge)[base]
        if u.id not in holders:raise RuntimeError('Resource release without acquisition')
        holders.remove(u.id)
        self.emit('resource_released',u,resource=resource,base_id=base)

    def advance(self,when):
        dt=when-self.t
        left,right=max(self.t,self.a),min(when,self.b)
        if right>left:
            for task in self.tasks.values():
                task.aoi_area+=((right-task.last)**2-(left-task.last)**2)/2
                task.overdue_area+=overdue_integral(task.last,left,right,task.tau)
        for u in self.fleet.values():
            used=u.power*dt/3600
            charged=u.charge_battery*dt/3600
            grid=u.charge_grid*dt/3600
            u.energy+=charged-used
            if u.energy < -1e-6:raise RuntimeError(f'Negative battery {u.id}: {u.energy}')
            u.energy=max(0.,u.energy)
            if u.energy>u.profile['battery_usable_wh']+1e-6:raise RuntimeError('Overcharged battery')
            self.consumed+=used;self.charged+=charged;self.grid+=grid
            self.eval_energy+=u.power*max(0.,right-left)/3600
            self.tail_energy+=u.power*max(0.,when-max(self.t,self.b))/3600
            meter=self.per_energy[u.id]
            meter['consumed_wh']+=used;meter['charged_battery_wh']+=charged;meter['grid_input_wh']+=grid
            if u.phase in MOVING:
                u.pos=(u.pos[0]+u.velocity[0]*dt,u.pos[1]+u.velocity[1]*dt)
            elif u.phase=='service':self.tasks[u.task].progress+=dt
        self.t=when

    def wind(self,point):return self.weather.wind(zone_at(point,self.centers),self.t)

    def straight(self,start,end,profile):
        total=0.
        for a,b,zone in split_leg(start,end,self.centers):
            speed=ground_speed(profile,a,b,self.weather.wind(zone,self.t))
            if speed is None:return None
            total+=hypot(b[0]-a[0],b[1]-a[1])/speed
        return total

    def route(self,u,start,node,end_node):
        """Return a current-field path over allowed directed graph connections."""
        end=self.positions[end_node]
        if hypot(start[0]-end[0],start[1]-end[1])<=EPS:return (0.,[end_node])
        if self.allowed is None:
            dt=self.straight(start,end,u.profile)
            return None if dt is None else (dt,[end_node])
        # Mid-leg recovery can use either endpoint, with all extra distance charged.
        origins=[(0.,node)]
        if hypot(start[0]-self.positions[node][0],start[1]-self.positions[node][1])>EPS:
            endpoints={node}
            if u.destination is not None:endpoints.add(u.destination)
            origins=[]
            for endpoint in sorted(endpoints):
                dt=self.straight(start,self.positions[endpoint],u.profile)
                if dt is not None:origins.append((dt,endpoint))
        queue=[]
        for duration,origin in origins:heappush(queue,(duration,origin,[origin]))
        visited=set()
        while queue:
            duration,current,path=heappop(queue)
            if current in visited:continue
            visited.add(current)
            if current==end_node:
                if path and hypot(start[0]-self.positions[path[0]][0],start[1]-self.positions[path[0]][1])<=EPS:path=path[1:]
                return duration,path
            for dest in self.adjacency.get(current,[]):
                if dest in visited:continue
                dt=self.straight(self.positions[current],self.positions[dest],u.profile)
                if dt is not None:heappush(queue,(duration+dt,dest,path+[dest]))
        return None

    def charging_estimate(self,u,base_id,arrival,arrival_energy):
        base=self.bases[base_id]
        slots=[self.t for _ in range(base['charge_ports'])]
        for i,other_id in enumerate(sorted(self.charge[base_id])):
            slots[i]=self.fleet[other_id].due or self.t
        for other_id in self.charge_queue[base_id]:
            other=self.fleet[other_id]
            power=min(base['charge_efficiency']*base['port_input_w'],other.profile['charge_intake_battery_w'])
            need=max(0.,base['target_soc']*other.profile['battery_usable_wh']-other.energy)
            i=min(range(len(slots)),key=lambda k:(slots[k],k))
            slots[i]+=need/power*3600
        start=max(arrival,min(slots))
        power=min(base['charge_efficiency']*base['port_input_w'],u.profile['charge_intake_battery_w'])
        need=max(0.,base['target_soc']*u.profile['battery_usable_wh']-arrival_energy)
        return start-arrival,need/power*3600

    def recoveries(self,u,start,node,energy,offset=0.,reserve=True):
        allowed=[u.home] if self.cfg['recovery']=='HOME' else sorted(self.bases)
        options=[]
        for base_id in allowed:
            base=self.bases[base_id]
            if hypot(*self.wind(self.positions[base['position_id']]))>u.profile['max_flight_wind_mps']+EPS:continue
            leg=self.route(u,start,node,base['position_id'])
            if leg is None:continue
            flight,path=leg
            flight_and_landing=(flight*u.profile['cruise_w']+base['landing_s']*u.profile['landing_w'])/3600
            arrival=self.t+offset+flight
            # Current pad work and queued landings are visible; future arrivals are not.
            pad_free=[self.t for _ in range(base['pad_capacity'])]
            for i,k in enumerate(sorted(self.pad[base_id])):pad_free[i]=self.fleet[k].due or self.t
            for _ in self.land_queue[base_id]:
                i=min(range(len(pad_free)),key=lambda x:(pad_free[x],x));pad_free[i]+=base['landing_s']
            pad_wait=max(0.,min(pad_free)-arrival)
            if pad_wait>base['max_landing_wait_s']+EPS:continue
            consumed=flight_and_landing+pad_wait*u.profile['hold_w']/3600
            if consumed+(u.profile['return_reserve_wh'] if reserve else 0)>energy+EPS:continue
            ready_ground=arrival+pad_wait+base['landing_s']+base['turnaround_s']
            arrival_energy=energy-consumed
            wait,charge=self.charging_estimate(u,base_id,ready_ground,arrival_energy)
            ready=ready_ground+wait+charge
            options.append(dict(base=base_id,flight_s=flight,path=path,energy_wh=consumed,
                                ready_s=ready,charging_wait_s=wait,charge_s=charge))
        if self.cfg['recovery']=='QUEUE':options.sort(key=lambda x:(x['ready_s'],x['base']))
        else:options.sort(key=lambda x:(x['flight_s'],x['base']))
        return options

    def assess(self,u,task,start=None,node=None,energy=None,offset=0.,visited=None):
        self.stats['candidate_scans']+=1
        if not task.active or task.owner not in {None,u.id}:return None
        steps=self.templates[task.template][task.step:]
        if not {step['capability'] for step in steps}<=set(u.profile['capabilities']):return None
        visited=(set() if u.phase=='idle' else u.visited) if visited is None else visited
        if len(visited|{task.node})>self.cfg['max_targets_per_sortie']:return None
        start=u.pos if start is None else start;node=u.node if node is None else node
        energy=u.energy if energy is None else energy
        target=self.positions[task.node]
        if any(hypot(*self.wind(target))>step['wind_limit_mps']+EPS for step in steps):return None
        leg=self.route(u,start,node,task.node)
        if leg is None:return None
        travel,path=leg
        takeoff=0.;takeoff_energy=0.
        if u.phase=='idle' and offset==0:
            base=self.bases[u.base]
            if hypot(*self.wind(start))>u.profile['max_flight_wind_mps']+EPS:return None
            takeoff=base['takeoff_s'];takeoff_energy=takeoff*u.profile['takeoff_w']/3600
        durations=[step['duration_s'] for step in steps]
        durations[0]=max(0.,durations[0]-task.progress)
        service=sum(durations)
        consumed=(travel*u.profile['cruise_w']+
                  sum(duration*u.profile['inspection_base_w']*step.get('power_factor',1)
                      for duration,step in zip(durations,steps)))/3600+takeoff_energy
        options=self.recoveries(u,target,task.node,energy-consumed,offset+takeoff+travel+service)
        if not options:return None
        return dict(task=task,travel_s=travel+takeoff,service_s=service,energy_wh=consumed,
                    completion_s=self.t+offset+takeoff+travel+service,recovery=options[0],path=path,
                    visited=visited|{task.node},remaining_energy=energy-consumed)

    def priority(self,task):
        if self.business=='batch':return (task.deadline,-task.priority,task.id)
        overdue=max(0.,(self.t-task.last)/task.tau-1)
        return (-overdue,task.deadline,-task.priority,task.id)

    def probability(self,task,additional):
        zone=zone_at(self.positions[task.node],self.centers)
        limit=min(float(step['wind_limit_mps']) for step in self.templates[task.template][task.step:])
        spells=self.cfg.get('reliability',{}).get(zone,{}).get(str(limit),[])
        age=self.weather.open_age(zone,limit,self.t)
        remaining=len(spells)-bisect_left(spells,age)
        if remaining<5:return 1.
        return (len(spells)-bisect_left(spells,age+additional))/remaining

    def choose(self,u):
        self.stats['decisions']+=1
        candidates=[]
        for task in sorted(self.tasks.values(),key=self.priority):
            candidate=self.assess(u,task)
            if candidate is not None:candidates.append(candidate)
        if not candidates:return None
        if self.cfg['policy']=='B0':
            if self.business=='batch':
                candidates.sort(key=lambda c:(c['completion_s']>c['task'].deadline,*self.priority(c['task'])))
            return candidates[0]
        plans=[(c,) for c in candidates[:6]]
        if self.cfg['max_targets_per_sortie']>1:
            for first in candidates[:6]:
                for next_task in sorted(self.tasks.values(),key=self.priority):
                    if next_task.id==first['task'].id:continue
                    follow=self.assess(u,next_task,self.positions[first['task'].node],first['task'].node,
                        first['remaining_energy'],first['completion_s']-self.t,first['visited'])
                    if follow is not None:
                        plans.append((first,follow));break
        plans.extend((c,) for c in candidates[6:6+max(0,12-len(plans))])
        def score(plan):
            benefit=0.;probability=1.
            for c in plan:
                task=c['task'];finish=c['completion_s']
                probability=min(probability,self.probability(task,finish-self.t))
                if self.business=='batch':gain=float(finish<=task.deadline)
                else:
                    horizon=self.t+7200
                    gain=0. if finish>=horizon else overdue_integral(task.last,finish,horizon,task.tau)-overdue_integral(finish,finish,horizon,task.tau)
                benefit+=probability*gain*task.priority
            return (-benefit,plan[-1]['recovery']['ready_s'],sum(c['energy_wh'] for c in plan),tuple(c['task'].id for c in plan))
        best=min(plans,key=score)
        return best[0] if score(best)[0]<-EPS else None

    def interrupt(self,u,reason):
        if u.task is None:return
        task=self.tasks[u.task]
        discarded=0. if self.cfg['execution_mode']=='accumulating' else task.progress
        if self.cfg['execution_mode']!='accumulating':task.progress=0.
        task.owner=None
        self.stats['service_interruptions']+=1
        self.stats['discarded_progress_s']+=discarded
        self.emit('service_interrupted',u,task_id=task.id,step_index=task.step,
                  discarded_s=discarded,retained_steps=task.step,reason=reason)
        self.emit('task_released',u,task_id=task.id)
        u.task=None

    def fail(self,u,reason):
        if u.failed is not None:return
        self.interrupt(u,reason)
        for b in self.bases:
            if u.id in self.pad[b]:self.release(u,'pad',b)
            if u.id in self.charge[b]:self.release(u,'charge',b)
            if u.id in self.land_queue[b]:self.land_queue[b].remove(u.id)
            if u.id in self.charge_queue[b]:self.charge_queue[b].remove(u.id)
        if u.id in self.air:self.release(u,'air')
        u.failed=reason
        self.set_phase(u,'failed')
        self.emit('aircraft_failed',u,reason=reason,position=list(u.pos),energy_wh=u.energy)
        if u.sortie_origin is not None:
            self.emit('sortie_ended',u,outcome='failed',origin_base_id=u.sortie_origin)
            u.sortie_origin=None

    def recover(self,u,reason):
        self.interrupt(u,reason)
        if u.phase=='takeoff' and u.base is not None and u.id in self.pad[u.base]:self.release(u,'pad',u.base)
        options=self.recoveries(u,u.pos,u.node,u.energy)
        if not options:
            options=self.recoveries(u,u.pos,u.node,u.energy,reserve=False)
            if options and not u.safety_recorded:
                u.safety_recorded=True;self.stats['safety_violations']+=1
                self.emit('energy_safety_violation',u,reason='recovery_reserve_shortfall',energy_wh=u.energy)
        if not options:
            self.fail(u,'no_feasible_recovery');return
        recovery=options[0]
        u.recovery=recovery['base']
        u.base=None
        self.emit('recovery_selected',u,base_id=u.recovery,reason=reason,
                  estimated_flight_s=recovery['flight_s'])
        self.start_flight(u,self.bases[u.recovery]['position_id'],'return')

    def start_flight(self,u,target,phase):
        leg=self.route(u,u.pos,u.node,target)
        if leg is None:
            if phase=='return':self.fail(u,'no_feasible_recovery')
            else:self.recover(u,'flight_weather_closed')
            return
        u.route_nodes=list(leg[1])
        u.final_destination=target
        u.phase=phase
        self.start_segment(u)

    def start_segment(self,u):
        while u.route_nodes and hypot(u.pos[0]-self.positions[u.route_nodes[0]][0],u.pos[1]-self.positions[u.route_nodes[0]][1])<=EPS:
            u.node=u.route_nodes.pop(0)
        if not u.route_nodes:
            if u.phase=='transit':
                u.visited.add(self.tasks[u.task].node)
                self.emit('target_arrived',u,task_id=u.task,position=list(u.pos))
                self.set_phase(u,'service_ready')
            else:
                self.arrive_base(u)
            return True
        hop=u.route_nodes[0]
        end=self.positions[hop]
        pieces=split_leg(u.pos,end,self.centers)
        start,segment_end,zone=pieces[0]
        speed=ground_speed(u.profile,start,segment_end,self.weather.wind(zone,self.t))
        if speed is None:
            self.recover(u,'flight_segment_closed');return True
        distance=hypot(segment_end[0]-u.pos[0],segment_end[1]-u.pos[1])
        if distance<=EPS:raise RuntimeError('Zero-duration motion segment')
        u.velocity=((segment_end[0]-u.pos[0])/distance*speed,(segment_end[1]-u.pos[1])/distance*speed)
        u.segment_end=segment_end
        u.destination=hop
        u.motion_pending=False
        self.set_phase(u,u.phase,distance/speed)
        return False

    def arrive_base(self,u):
        b=u.recovery
        u.pos=self.positions[self.bases[b]['position_id']]
        u.node=self.bases[b]['position_id']
        u.base=b
        u.queue_since=self.t
        u.hold_deadline=self.t+self.bases[b]['max_landing_wait_s']
        self.land_queue[b].append(u.id)
        self.set_phase(u,'landing_wait')
        self.schedule(u.hold_deadline,2,'hold_end',(u.id,u.stamp))
        self.emit('landing_queued',u,base_id=b)

    def start_service(self,u):
        task=self.tasks[u.task]
        assessment=self.assess(u,task)
        if assessment is None:
            self.recover(u,'service_admission_failed');return
        step=self.templates[task.template][task.step]
        remaining=max(0.,step['duration_s']-task.progress)
        self.set_phase(u,'service',remaining)
        # An explicit energy safeguard precedes depletion even if no weather event occurs.
        recovery=self.recoveries(u,u.pos,u.node,u.energy)
        if recovery:
            margin=u.energy-u.profile['return_reserve_wh']-recovery[0]['energy_wh']
            safeguard=max(0.,margin)/u.power*3600 if u.power else remaining
            if safeguard < remaining-EPS:
                self.schedule(self.t+safeguard,1,'safeguard',(u.id,u.stamp))

    def complete_phase(self,u):
        phase=u.phase
        if phase=='takeoff':
            self.release(u,'pad',u.base)
            u.base=None
            # Weather may have closed sensing during the fixed takeoff phase.
            # Check again before committing the outbound leg, just as at every
            # other service/flight transition.
            if self.assess(u,self.tasks[u.task]) is None:self.recover(u,'post_takeoff_admission_failed')
            else:self.start_flight(u,self.tasks[u.task].node,'transit')
            return True
        if phase in MOVING:
            u.pos=u.segment_end
            u.motion_pending=True
            if u.route_nodes and hypot(u.pos[0]-self.positions[u.route_nodes[0]][0],u.pos[1]-self.positions[u.route_nodes[0]][1])<=EPS:
                u.node=u.route_nodes.pop(0)
            # A pure zone boundary is not a dispatch trigger.
            if not u.route_nodes:
                if phase=='transit':
                    u.visited.add(self.tasks[u.task].node)
                    self.emit('target_arrived',u,task_id=u.task,position=list(u.pos))
                    self.set_phase(u,'service_ready')
                else:self.arrive_base(u)
                return True
            return False
        if phase=='service':
            task=self.tasks[u.task]
            self.emit('step_completed',u,task_id=task.id,step_index=task.step,retained_steps=task.step+1)
            self.stats['steps_completed']+=1
            task.step+=1;task.progress=0.
            if task.step==len(self.templates[task.template]):
                task.last=self.t;task.active=False;task.owner=None;task.step=0;task.generation+=1
                in_eval=self.a-EPS<=self.t<=self.b+EPS
                if in_eval:task.updates+=1
                record=dict(task_id=task.id,time_s=self.t,aircraft_id=u.id,generation=task.generation,in_evaluation=in_eval)
                self.completions.append(record)
                self.emit('inspection_completed',u,task_id=task.id,generation=task.generation)
                self.emit('task_released',u,task_id=task.id)
                if self.business=='periodic':
                    task.release=self.t+.5*task.tau;task.deadline=self.t+task.tau
                    self.schedule(task.release,3,'release',task.id)
                    self.schedule(task.deadline,3,'deadline',(task.id,task.generation))
                u.task=None
                self.set_phase(u,'decision')
            else:self.set_phase(u,'service_ready')
            return True
        if phase=='landing':
            self.release(u,'pad',u.base);self.release(u,'air')
            if u.sortie_origin!=u.base:self.stats['cross_base_recoveries']+=1
            self.emit('sortie_ended',u,outcome='landed',origin_base_id=u.sortie_origin,base_id=u.base,
                      visited_positions=sorted(u.visited))
            u.sortie_origin=None
            self.set_phase(u,'turnaround',self.bases[u.base]['turnaround_s'])
            return True
        if phase=='turnaround':
            u.queue_since=self.t
            self.charge_queue[u.base].append(u.id)
            self.set_phase(u,'charge_wait')
            self.emit('charging_queued',u,base_id=u.base)
            return True
        if phase=='charging':
            u.energy=self.bases[u.base]['target_soc']*u.profile['battery_usable_wh']
            self.release(u,'charge',u.base)
            self.set_phase(u,'idle')
            return True
        raise RuntimeError(f'Unexpected phase completion: {phase}')

    def settle(self,dispatch):
        # Zero-time transitions are settled before advancing the calendar.
        for _ in range(10000):
            changed=False
            for b,queue in self.land_queue.items():
                while queue and len(self.pad[b])<self.bases[b]['pad_capacity']:
                    u=self.fleet[queue.pop(0)]
                    if u.failed is not None:continue
                    if hypot(*self.wind(u.pos))>u.profile['max_flight_wind_mps']+EPS:
                        self.fail(u,'landing_weather_closed');changed=True;continue
                    landing=self.bases[b]['landing_s']
                    if u.energy+EPS<landing*u.profile['landing_w']/3600:
                        self.fail(u,'insufficient_landing_energy');changed=True;continue
                    self.stats['landing_wait_s']+=self.t-u.queue_since
                    self.acquire(u,'pad',b)
                    self.set_phase(u,'landing',landing)
                    changed=True
                for key in list(queue):
                    u=self.fleet[key]
                    if u.hold_deadline is not None and self.t>=u.hold_deadline-EPS:
                        self.fail(u,'landing_wait_exceeded');changed=True
            for b,queue in self.charge_queue.items():
                while queue and (self.cfg['execution_mode']=='instant_charge' or len(self.charge[b])<self.bases[b]['charge_ports']):
                    u=self.fleet[queue.pop(0)]
                    if u.failed is not None:continue
                    self.stats['charging_wait_s']+=self.t-u.queue_since
                    need=max(0.,self.bases[b]['target_soc']*u.profile['battery_usable_wh']-u.energy)
                    if self.cfg['execution_mode']=='instant_charge':
                        grid=need/self.bases[b]['charge_efficiency']
                        u.energy+=need;self.charged+=need;self.grid+=grid
                        self.per_energy[u.id]['charged_battery_wh']+=need
                        self.per_energy[u.id]['grid_input_wh']+=grid
                        self.emit('instant_charge',u,battery_wh=need,grid_wh=grid,energy_wh=u.energy,base_id=b)
                        self.set_phase(u,'idle')
                    elif need<=EPS:self.set_phase(u,'idle')
                    else:
                        power=min(self.bases[b]['charge_efficiency']*self.bases[b]['port_input_w'],u.profile['charge_intake_battery_w'])
                        self.acquire(u,'charge',b)
                        self.set_phase(u,'charging',need/power*3600)
                    changed=True
            for u in self.fleet.values():
                if u.phase in MOVING and getattr(u,'motion_pending',False):
                    changed=self.start_segment(u) or changed
                elif u.phase=='service_ready':self.start_service(u);changed=True
                elif u.phase=='decision':
                    candidate=None if self.closed else self.choose(u)
                    if candidate is None:self.recover(u,'no_continuation')
                    else:self.assign(u,candidate,ground=False)
                    changed=True
            if dispatch and not self.closed:
                for u in self.fleet.values():
                    if u.phase!='idle':continue
                    target=self.bases[u.base]['target_soc']*u.profile['battery_usable_wh']
                    if u.energy+EPS<target:
                        u.queue_since=self.t;self.charge_queue[u.base].append(u.id)
                        self.set_phase(u,'charge_wait');changed=True;continue
                    if len(self.air)>=self.cfg['air_capacity'] or len(self.pad[u.base])>=self.bases[u.base]['pad_capacity']:continue
                    candidate=self.choose(u)
                    if candidate is None:continue
                    self.assign(u,candidate,ground=True);changed=True
            if not changed:return
        raise RuntimeError('Non-terminating zero-time transition loop')

    def assign(self,u,candidate,ground):
        task=candidate['task']
        if task.owner is not None:raise RuntimeError('Task double assignment')
        task.owner=u.id;u.task=task.id
        self.emit('task_assigned',u,task_id=task.id)
        if ground:
            u.visited=set();u.sortie+=1;u.safety_recorded=False
            u.sortie_origin=u.base;u.recovery=None
            self.stats['sorties']+=1
            self.emit('sortie_started',u,base_id=u.base,sortie_number=u.sortie)
            self.acquire(u,'air');self.acquire(u,'pad',u.base)
            self.set_phase(u,'takeoff',self.bases[u.base]['takeoff_s'])
        else:self.start_flight(u,task.node,'transit')

    def weather_changed(self):
        self.route_cache.clear()
        for u in self.fleet.values():
            if u.failed:continue
            if u.phase in {'service','service_ready'}:
                if self.assess(u,self.tasks[u.task]) is None:self.recover(u,'service_weather_or_energy')
            elif u.phase=='transit':
                candidate=self.assess(u,self.tasks[u.task])
                if candidate is None:self.recover(u,'transit_weather_or_energy')
                else:self.start_flight(u,self.tasks[u.task].node,'transit')
            elif u.phase=='return':self.recover(u,'return_weather_changed')
            elif u.phase=='takeoff' and hypot(*self.wind(u.pos))>u.profile['max_flight_wind_mps']+EPS:
                self.recover(u,'takeoff_weather_closed')
            elif u.phase in {'landing','landing_wait'} and hypot(*self.wind(u.pos))>u.profile['max_flight_wind_mps']+EPS:
                self.fail(u,'landing_weather_closed')

    def run(self):
        if self._running:raise RuntimeError('Simulation is single-use')
        self._running=True
        previous_field=None
        for u in self.fleet.values():self.set_phase(u,'idle')
        self.schedule(0.,3,'startup')
        boundaries=sorted({float(row['start_s']) for rows in self.weather.series.values() for row in rows if 0<=row['start_s']<=self.end})
        for t in boundaries:self.schedule(t,2,'weather')
        for task in self.tasks.values():
            self.schedule(max(0.,task.release),3,'release',task.id)
            if task.deadline>=0:self.schedule(task.deadline,3,'deadline',(task.id,task.generation))
        self.schedule(self.a,4,'evaluation_start')
        self.schedule(self.b,4,'close')
        self.schedule(self.end,5,'end')
        while self.calendar:
            # Stale events must not create extra dispatch opportunities.
            while self.calendar:
                event=self.calendar[0]
                if event[3] in {'finish','empty','hold_end','safeguard','reserve'}:
                    key,stamp=event[4]
                    if self.fleet[key].stamp!=stamp:heappop(self.calendar);continue
                elif event[3]=='deadline':
                    key,generation=event[4]
                    if self.tasks[key].generation!=generation or not self.tasks[key].active:
                        heappop(self.calendar);continue
                break
            if not self.calendar:break
            when=self.calendar[0][0]
            self.advance(when)
            dispatch=False
            while self.calendar and abs(self.calendar[0][0]-self.t)<=1e-9:
                _,_,_,kind,payload=heappop(self.calendar)
                if kind in {'finish','empty','hold_end','safeguard','reserve'}:
                    key,stamp=payload;u=self.fleet[key]
                    if u.stamp!=stamp:continue
                    if kind=='finish':dispatch=self.complete_phase(u) or dispatch
                    elif kind=='empty':self.fail(u,'energy_depleted');dispatch=True
                    elif kind=='reserve':
                        if u.phase in AIR:
                            self.emit('reserve_crossed',u,phase=u.phase,energy_wh=u.energy)
                            if not u.safety_recorded:
                                u.safety_recorded=True;self.stats['safety_violations']+=1
                                self.emit('energy_safety_violation',u,reason='actual_reserve_crossing',energy_wh=u.energy)
                            if u.phase not in {'return','landing','landing_wait'}:
                                self.recover(u,'actual_reserve_crossing');dispatch=True
                    elif kind=='hold_end':
                        # Let landings take a pad released at this exact instant.
                        dispatch=True
                    else:self.recover(u,'service_energy_safeguard');dispatch=True
                elif kind=='weather':
                    field=tuple(self.weather.wind(z,self.t) for z in sorted(self.centers))
                    if field!=previous_field:
                        self.emit('weather_changed')
                        self.weather_changed();dispatch=True
                    previous_field=field
                elif kind=='release':
                    task=self.tasks[payload]
                    if not self.closed and self.t<self.b:
                        task.active=True;dispatch=True
                        self.emit('task_available',task_id=task.id,generation=task.generation)
                elif kind=='deadline':
                    key,generation=payload;task=self.tasks[key]
                    if task.active and task.generation==generation and not self.closed:
                        self.emit('task_deadline',task_id=task.id,generation=generation)
                        dispatch=True
                elif kind=='close':
                    self.closed=True
                    for u in self.fleet.values():
                        if u.phase in AIR and u.phase not in {'return','landing','landing_wait'}:self.recover(u,'evaluation_end')
                    self.emit('evaluation_closed')
                elif kind=='end':return self.result()
                elif kind=='evaluation_start':self.emit('evaluation_started')
                elif kind=='startup':dispatch=True
            self.settle(dispatch)
        raise RuntimeError('Calendar ended before recovery boundary')

    def result(self):
        duration=self.b-self.a;m=len(self.tasks)
        normalized=sorted(task.aoi_area/duration/task.tau for task in self.tasks.values())
        index=.9*(len(normalized)-1);lo=int(index);hi=min(lo+1,len(normalized)-1)
        percentile=normalized[lo]+(normalized[hi]-normalized[lo])*(index-lo)
        terminal_energy=sum(u.energy for u in self.fleet.values())
        balance=self.initial_energy+self.charged-self.consumed-terminal_energy
        for key,u in self.fleet.items():self.per_energy[key]['terminal_wh']=u.energy
        failures=sum(u.failed is not None for u in self.fleet.values())
        unrecovered=sum(u.phase in AIR for u in self.fleet.values())
        metrics=dict(mean_aoi_s=sum(t.aoi_area for t in self.tasks.values())/(duration*m),
            normalized_overdue=sum(t.overdue_area for t in self.tasks.values())/(duration*m),
            mean_normalized_age=sum(normalized)/m,p90_normalized_age=percentile,
            operational_energy_wh=self.eval_energy+self.tail_energy,
            complete_inspections=sum(t.updates for t in self.tasks.values()),
            zero_update_fraction=sum(t.updates==0 for t in self.tasks.values())/m,
            aircraft_failures=failures,unrecovered_aircraft=unrecovered,**self.stats)
        terminal=dict(time_s=self.t,aircraft=[dict(id=u.id,phase=u.phase,position=list(u.pos),energy_wh=u.energy,
            home_base_id=u.home,current_base_id=u.base,recovery_base_id=u.recovery,failed_reason=u.failed)
            for u in self.fleet.values()],tasks=[dict(id=t.id,last_completion_s=t.last,step_index=t.step,
                progress_s=t.progress,owner=t.owner,active=t.active,updates=t.updates,
                aoi_area_s2=t.aoi_area,overdue_area_s=t.overdue_area) for t in self.tasks.values()])
        return dict(scenario_id=self.s.get('scenario_id','unnamed'),config=self.cfg,metrics=metrics,
            completions=self.completions,events=self.events,terminal=terminal,
            metric_windows=dict(service='evaluation_start_s through evaluation_end_s',
                operational_energy='evaluation plus recovery',process='whole run including warm-up',
                process_evaluation='derive clipped durations and event counts from events'),
            resource_audit=dict(max_occupancy=self.max_occupancy,air_capacity=self.cfg['air_capacity'],
                                final_air=sorted(self.air),final_pad={b:sorted(x) for b,x in self.pad.items()},
                                final_charge={b:sorted(x) for b,x in self.charge.items()}),
            energy_ledger=dict(initial_wh=self.initial_energy,charged_battery_wh=self.charged,
                consumed_wh=self.consumed,terminal_wh=terminal_energy,lost_wh=0.,balance_error_wh=balance,
                grid_input_wh=self.grid,evaluation_consumed_wh=self.eval_energy,
                recovery_consumed_wh=self.tail_energy,per_aircraft=self.per_energy))


def simulate(scenario,config=None,weather=None):
    return Simulation(scenario,config,weather).run()
