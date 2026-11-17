"""Record two GIFs from actual browser screenshots of genuine historical replays.

Start `island-uav serve --port 8765` first. This optional documentation tool
requires Pillow and an installed Playwright CLI executable (--browser-cli).
The simulator itself needs none of these packages.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser-cli', default='playwright-cli', help='Playwright CLI executable or wrapper path')
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--session', default='island-uav-recording')
    args = parser.parse_args()
    prefix = [str(args.browser_cli), f'-s={args.session}']
    def cli(*parts):
        process = subprocess.run(prefix+list(parts), cwd=ROOT, text=True, capture_output=True, check=True)
        if '### Error' in process.stdout:
            raise RuntimeError(process.stdout)
        return process.stdout
    cli('open', args.url)
    cli('resize', '1440', '960')
    cli('snapshot')
    records = []
    # A short window containing genuine departures, sensing and recovery. The
    # compact historical wind window opens later, so it starts after 3 h.
    for name, network, fleet, recovery, start, end in [
        ('single-base', 'legacy_24', 4, 'HOME', 10800, 12600),
        ('multi-base', 'pingtan_96', 12, 'NEAR', 0, 1800),
    ]:
        out = ROOT/'output/playwright'/name
        out.mkdir(parents=True, exist_ok=True)
        code = """async (page) => {
          await page.waitForFunction(() => window.islandApp?.getReplay());
          await page.locator('#network_id').selectOption(NETWORK);
          await page.locator('#fleet_size').selectOption(FLEET);
          await page.locator('#weather').selectOption('historical');
          if(!await page.locator('#recovery_policy').isDisabled()) await page.locator('#recovery_policy').selectOption(RECOVERY);
          await page.locator('#duration_hours').selectOption('6');
          await page.locator('#run-button').click();
          await page.waitForFunction(() => !document.getElementById('run-button').disabled && !document.getElementById('run-error').textContent);
          await page.evaluate(() => window.islandApp.pause());
          const replay = await page.evaluate(() => {
            const r=window.islandApp.getReplay();
            return {metadata:r.metadata,sites:r.sites.length,bases:r.bases.length,aircraft:r.aircraft.length,frames:r.frames.length,events:r.events.length,
              complete:r.frames.at(-1).completed,crossBase:r.summary.collection.cross_base_recoveries};
          });
          console.log(JSON.stringify(replay));
          for(let t=START,i=0;t<=END;t+=30,i++){
            const check=await page.evaluate(t=>{
              window.islandApp.seek(t);
              const f=window.islandApp.getReplay().frames.find(f=>Math.abs(f.t-t)<1e-6);
              const c=Number(document.getElementById('metric-age').textContent),p=Number(document.getElementById('metric-platform-age').textContent);
              return {c,p,expectedC:f?.age_h,expectedP:f?.platform_age_h};
            },t);
            if(!Number.isFinite(check.c)||!Number.isFinite(check.p)||Math.abs(check.c-check.expectedC)>.00501||Math.abs(check.p-check.expectedP)>.00501) throw new Error('Browser information ages do not match the event-derived samples: '+JSON.stringify(check));
            await page.screenshot({path:DIR+'/'+String(i).padStart(3,'0')+'.png'});
          }
          await page.evaluate(t=>window.islandApp.seek(t), START+600);
          await page.screenshot({path:STILL}); return replay;
        }"""
        replacements = {'NETWORK':network, 'FLEET':str(fleet), 'RECOVERY':recovery,
                        'START':start, 'END':end, 'DIR':str(out),
                        'STILL':str(ROOT/'docs'/f'{name}.png')}
        for key,value in replacements.items():
            code = code.replace(key, json.dumps(value))
        result = cli('run-code', code)
        (out/'browser-capture.log').write_text(result)
        paths = sorted(out.glob('[0-9][0-9][0-9].png'))
        images = [Image.open(path).convert('RGB') for path in paths]
        # One palette for all real screenshots prevents colour flicker.
        sample = Image.new('RGB', (1440,960*3))
        for i,idx in enumerate([0,len(images)//2,len(images)-1]):
            sample.paste(images[idx], (0,960*i))
        palette = sample.quantize(colors=224)
        frames = [image.quantize(palette=palette,dither=Image.Dither.NONE) for image in images]
        target = ROOT/'docs'/f'{name}.gif'
        frames[0].save(target,save_all=True,append_images=frames[1:],duration=100,loop=0,optimize=True,disposal=1)
        records.append({'file':f'docs/{name}.gif','sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                        'network_id':network,'fleet_size':fleet,'weather':'historical','historical_block':'2025-04-04',
                        'recovery_policy':recovery,'operating_hours':6,'warmup_hours':0,'recovery_hours':1,
                        'initial_age_mode':'due','recorded_simulation_seconds':[start,end],
                        'screenshot_interval_s':30,'gif_frame_duration_ms':100,'frames':len(frames),
                        'browser_validation':'Both displayed AoI values finite and agree with independent backend frames to display precision at every captured frame.',
                        'method':'Actual localhost browser screenshots; only global palette quantisation and GIF encoding.'})
        print(f'{target}: {len(frames)} actual screenshots, {target.stat().st_size:,} bytes')
    (ROOT/'docs/interface.png').write_bytes((ROOT/'docs/multi-base.png').read_bytes())
    (ROOT/'docs/visualization_provenance.json').write_text(json.dumps({'recordings':records,
        'scope':'Short interface demonstrations using historical wind; these initial ages and horizons are not the full manuscript evaluation protocol.',
        'map':'Offline OSM vector geometry; see map_provenance.json.'},indent=2)+'\n')
    cli('close')

if __name__ == '__main__':
    main()
