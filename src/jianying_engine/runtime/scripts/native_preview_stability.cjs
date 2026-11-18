// A frame can have the requested timestamp before all layers finish rendering.
function observe(state, pixels, now, minimumMs){
 if(state.previous&&pixels.equals(state.previous))state.reads++;
 else{state.reads=1;state.since=now;}
 state.previous=Buffer.from(pixels);
 return state.reads>=3&&now-state.since>=minimumMs;
}
module.exports={observe};
