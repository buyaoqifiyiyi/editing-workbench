'use strict';
// Local adapter for Mesh Avatar Studio's MIT-licensed runtime; the runtime stays in the caller's installation.
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

async function main() {
  const [studio, avatar, output, configFile, browserPath] = process.argv.slice(2);
  const config = JSON.parse(fs.readFileSync(configFile, 'utf8'));
  const runtime = path.join(studio, 'packages', 'runtime', 'dist', 'index.js');
  const { chromium } = require(path.join(studio, 'node_modules', 'playwright'));
  const html = `<!doctype html><meta charset="utf-8"><style>html,body{margin:0}canvas{width:1280px;height:720px}</style><canvas width="1280" height="720"></canvas><script type="module">
import {loadMeshAvatar,PARAMS} from '/runtime.js';
try {
 const config=await (await fetch('/config')).json();
 const avatar=await loadMeshAvatar(document.querySelector('canvas'),'/avatar.mavatar',{manual:true,fit:'contain'});
 const defaults=Object.fromEntries(PARAMS.map(p=>[p.id,p.def]));
 avatar.setAutoMotion(false);avatar.setAutoIdle(false);
 window.renderFrame=(i)=>{
  const t=i/30; const keys=config.keyframes; let values={...defaults};
  for (const [name,points] of Object.entries(keys)) {
   let left=points[0],right=points[points.length-1];
   for(let k=1;k<points.length;k++) if(t<points[k].time){left=points[k-1];right=points[k];break;}
   let f=right.time===left.time?0:Math.max(0,Math.min(1,(t-left.time)/(right.time-left.time)));
   values[name]=left.value+(right.value-left.value)*f;
  }
  avatar.setParameters(values);avatar.advance(1/30,30);
  return document.querySelector('canvas').toDataURL('image/png').split(',')[1];
 }; document.body.dataset.ready='yes';
} catch(e){document.body.dataset.error=String(e.stack||e)}
</script>`;
  const server = http.createServer((req, res) => {
    const files = {'/runtime.js': [runtime, 'text/javascript'], '/avatar.mavatar': [avatar, 'application/octet-stream'], '/config': [configFile, 'application/json']};
    if (req.url === '/') {res.writeHead(200, {'Content-Type':'text/html'}); return res.end(html);}
    const entry=files[req.url]; if (!entry) {res.writeHead(404);return res.end();}
    res.writeHead(200, {'Content-Type':entry[1]}); fs.createReadStream(entry[0]).pipe(res);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser=await chromium.launch({headless:true,executablePath:browserPath});
    const page=await browser.newPage({viewport:{width:1280,height:720},deviceScaleFactor:1});
    const errors=[]; page.on('pageerror',e=>errors.push(e.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(()=>document.body.dataset.ready||document.body.dataset.error,{timeout:30000});
    const error=await page.locator('body').getAttribute('data-error'); if(error)throw Error(error);
    for(let i=0;i<config.frames;i++) {
      const frame=await page.evaluate(i=>window.renderFrame(i),i);
      fs.writeFileSync(path.join(output,`${String(i).padStart(5,'0')}.png`),Buffer.from(frame,'base64'));
    }
    if(errors.length)throw Error(errors.join('\n'));
    process.stdout.write(JSON.stringify({frames:config.frames,errors}));
  } finally {if(browser)await browser.close();server.close();}
}
main().catch(e=>{console.error(e.stack||String(e));process.exitCode=1});
