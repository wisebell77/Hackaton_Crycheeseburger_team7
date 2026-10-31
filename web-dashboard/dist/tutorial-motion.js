/* Cancellable motion shared by the tour and its deterministic timing tests. */
(function () {
  const clamp=(n,min,max)=>Math.max(min,Math.min(max,n));
  function duration(distance,reduced=false) { return reduced||distance<2?0:clamp(260+distance*.16,280,620); }
  function canConnect(a,b,sameSurface,height) {
    return !!(sameSurface&&a&&b&&Math.hypot(a.left-b.left,a.top-b.top)<height*.58&&Math.abs(a.width-b.width)<520);
  }
  function scrollTo(root,left,top,{reduced=false,cancelled=()=>false,onFrame=()=>{}}={}) {
    const x=root.scrollLeft,y=root.scrollTop;
    left=clamp(left,0,Math.max(0,root.scrollWidth-root.clientWidth));
    top=clamp(top,0,Math.max(0,root.scrollHeight-root.clientHeight));
    const time=duration(Math.hypot(left-x,top-y),reduced);
    if(cancelled())return Promise.resolve(false);
    const write=(a,b)=>{root.scrollTo({left:a,top:b,behavior:'instant'});onFrame();};
    if(!time){write(left,top);return Promise.resolve(true);}
    return new Promise(resolve=>{
      let start=null;
      function frame(now) {
        if(cancelled()){resolve(false);return;}
        start??=now;const progress=Math.min(1,(now-start)/time),ease=progress<.5?4*progress**3:1-((-2*progress+2)**3)/2;
        write(x+(left-x)*ease,y+(top-y)*ease);
        if(progress<1)requestAnimationFrame(frame);else resolve(true);
      }
      requestAnimationFrame(frame);
    });
  }
  globalThis.TutorialMotion={duration,canConnect,scrollTo};
})();
