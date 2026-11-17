"""Build the offline OSM vector background from the research's frozen inputs."""
from pathlib import Path
import argparse, hashlib, json

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True,help='V5 data directory')
    args=parser.parse_args();root=Path(__file__).resolve().parents[1];data=args.source
    definitions=[('raw/geography/inherited/haitan_boundary.geojson','land'),
                 ('raw/geography/inherited/gis_osm_landuse_a_free_1.geojson','landuse'),
                 ('raw/geography/inherited/gis_osm_roads_free_1.geojson','roads'),
                 ('processed/geography/research_weather_zones.geojson','windzone')]
    features=[];sources=[]
    def compact(value):
        if isinstance(value,list):return [compact(x)for x in value]
        if isinstance(value,float):return round(value,6)
        return value
    for name,layer in definitions:
        path=data/name;payload=json.loads(path.read_text())
        sources.append({'source':name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'layer':layer,'features':len(payload['features'])})
        for f in payload['features']:
            props=f.get('properties',{});features.append({'type':'Feature','properties':{'layer':layer,**{k:props[k]for k in ['fclass','name','osm_id','id','zone_id']if k in props}},'geometry':{'type':f['geometry']['type'],'coordinates':compact(f['geometry']['coordinates'])}})
    result={'type':'FeatureCollection','attribution':'Map data © OpenStreetMap contributors / Geofabrik Fujian 2026-01-01. ODbL 1.0. Wind-zone overlays are research-derived nearest-representative areas.','features':features}
    out=root/'src/island_uav/web/map.geojson';out.write_text(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    manifest={'sources':sources,'output_sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'features':len(features),'licence':'ODbL-1.0','coordinate_rounding_decimals':6,'extent':'Full Pingtan study island; no V4 UI crop','source_url':'https://download.geofabrik.de/asia/china/fujian.html'}
    (root/'docs/map_provenance.json').write_text(json.dumps(manifest,indent=2)+'\n');print(json.dumps(manifest))
if __name__=='__main__':main()
