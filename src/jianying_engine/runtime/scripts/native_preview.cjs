// Owned read-only preview worker using the shipped N-API and termination contract.
const fs=require('fs'),path=require('path'),{pathToFileURL}=require('url');
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const record={pid:process.pid,success:false,steps:[],frames:[]};
const output=path.dirname(process.argv[2]);
function save(phase,data={}){
 record.steps.push({phase,time:new Date().toISOString(),...data});
 fs.writeFileSync(path.join(output,'checkpoint.json'),JSON.stringify(record,null,2));
}
function parsed(value){return typeof value==='string'?JSON.parse(value):value;}
async function main(){
 let addon,runtime,terminate,sessionId,sequence=0;
 async function invoke(method,params={}){
  const result=parsed(await addon.invoke(JSON.stringify({...params,method,params,sessionId,
   id:`skill-preview-${++sequence}`,protocolVersion:1})));
  save(method,{result});return result;
 }
 try{
  runtime=require(path.join(config.install,'agent_runtime_addon.node'));
  ({requestVideoEditorServiceSelfTermination:terminate}=await import(pathToFileURL(path.join(config.install,'Resources/canvas_agent/runtime/videoeditor-service.mjs')).href));
  if(typeof runtime.terminateProcess!=='function'||typeof terminate!=='function')throw Error('Shipped termination contract unavailable');
  addon=require(path.join(config.install,'videoeditor_addon.node'));
  const initialized=parsed(await addon.initialize(JSON.stringify({modelsDir:path.join(config.install,'Resources/models'),algorithmConfigPath:''})));
  save('initialize',{result:initialized});if(!initialized.admissible)throw Error('Native initialization rejected');
  sessionId=(await invoke('create',{kind:'draft',serviceGeneration:'1'})).sessionId;
  if(!sessionId)throw Error('Session not created');
  if(!(await invoke('load',{draftPath:config.draft,workspacePath:path.join(output,'workspace')})).accepted)throw Error('Load rejected');
  for(let index=0;index<config.times.length;index++){
   const targetUs=Math.round(config.times[index]*1e6);
   if(!(await invoke('seek',{positionUs:targetUs,seekMode:'done'})).accepted)throw Error('Seek rejected');
   if(!(await invoke('refreshCurrentFrame',{actionId:0,force:true})).accepted)throw Error('Refresh rejected');
   let captured=false,previousPixels=null,stableReads=0;
   for(let attempt=0;attempt<25;attempt++){
    await new Promise(resolve=>setTimeout(resolve,100));
    const packet=Buffer.from(await addon.readFrame(sessionId,320,180));
    if(packet.length<40)continue;
    if(packet.toString('ascii',0,4)!=='VEF1'||packet.readUInt16LE(4)!==1||packet.readUInt16LE(6)!==40||
      packet.readUInt32LE(8)!==320||packet.readUInt32LE(12)!==180||packet.readUInt32LE(20)!==1)throw Error('Unsupported frame packet');
    const stride=packet.readUInt32LE(16),timestampUs=Number(packet.readBigUInt64LE(24));
    if(stride!==1280||packet.length!==40+stride*180)throw Error('Invalid frame packet size');
    // A seek acknowledgement is insufficient: reject a stale frame from the previous position.
    if(Math.abs(timestampUs-targetUs)>config.toleranceUs){previousPixels=null;stableReads=0;continue;}
    // Timestamp alignment alone can precede composition refresh; require three identical RGBA reads.
    const pixels=packet.subarray(40);
    if(previousPixels&&pixels.equals(previousPixels))stableReads++;else stableReads=1;
    previousPixels=Buffer.from(pixels);
    if(stableReads<3)continue;
    const file=`frame-${index}.vef`;fs.writeFileSync(path.join(output,file),packet);
    record.frames.push({file,targetUs,timestampUs,sequence:packet.readBigUInt64LE(32).toString()});
    save('frame',{index,timestampUs,stableReads});captured=true;break;
   }
   if(!captured)throw Error('Requested position produced no current preview frame');
  }
  record.success=true;
 }catch(error){record.success=false;save('error',{message:error.stack});}
 finally{
  try{
   if(sessionId&&!(await invoke('destroy')).accepted)throw Error('Destroy rejected');
  }catch(error){record.success=false;save('cleanup_error',{message:error.stack});}
  try{
   if(addon){const stopped=parsed(await addon.shutdown());save('shutdown',{result:stopped});if(!stopped.stopped)throw Error('Shutdown rejected');}
  }catch(error){record.success=false;save('cleanup_error',{message:error.stack});}
 }
 save('calling_shipped_self_termination');
 if(terminate&&runtime)terminate(runtime.terminateProcess);
 record.success=false;save('termination_unexpectedly_returned');process.exitCode=1;
}
main().catch(error=>{record.success=false;save('error',{message:error.stack});process.exitCode=1;});
