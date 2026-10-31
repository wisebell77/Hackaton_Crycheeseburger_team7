const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const frames=[],box={requestAnimationFrame:fn=>frames.push(fn)};
vm.createContext(box);vm.runInContext(fs.readFileSync(path.join(__dirname,'../dist/tutorial-motion.js'),'utf8'),box);
const motion=box.TutorialMotion;
const pane=()=>({scrollLeft:0,scrollTop:0,clientWidth:400,clientHeight:300,scrollWidth:1000,scrollHeight:6000,writes:[],scrollTo(p){this.scrollLeft=p.left;this.scrollTop=p.top;this.writes.push([p.left,p.top]);}});
function drain(){let time=0;while(frames.length){frames.shift()(time);time+=16;if(time>10000)throw Error('motion did not settle');}}
(async()=>{
  const page=pane();const move=motion.scrollTo(page,200,2000);drain();assert.equal(await move,true);assert.equal(page.scrollTop,2000);assert.equal(page.scrollLeft,200);
  assert(page.writes.length>10,'long scroll must have intermediate frames');
  assert(page.writes.slice(1).every((p,i)=>p[1]>=page.writes[i][1]),'scroll must not jump backwards');
  assert(page.writes.some(p=>p[1]>0&&p[1]<2000),'must visibly pass through intermediate positions');
  let cancelled=false;const modal=pane(),scroll=motion.scrollTo(modal,9999,9999,{cancelled:()=>cancelled});
  frames.shift()(0);frames.shift()(16);frames.shift()(32);const before=modal.writes.length;cancelled=true;drain();assert.equal(await scroll,false);assert.equal(modal.writes.length,before,'exit must stop late scroll writes');
  const still=pane();assert.equal(await motion.scrollTo(still,2,4,{cancelled:()=>true}),false);assert.equal(still.writes.length,0);
  const reduced=pane();await motion.scrollTo(reduced,9999,9999,{reduced:true});assert.equal(reduced.writes.length,1);assert.equal(reduced.scrollTop,5700);assert.equal(reduced.scrollLeft,600);assert.equal(frames.length,0,'reduced motion must not animate');
  const a={left:20,top:100,width:350};assert.equal(motion.canConnect(a,{left:30,top:230,width:360},true,800),true);
  assert.equal(motion.canConnect(a,{left:30,top:230,width:360},false,800),false,'different pages must fade instead of sweeping the spotlight');
  assert.equal(motion.canConnect(a,{left:30,top:2400,width:360},true,800),false,'distant targets must not sweep the screen');
  assert(motion.duration(10000)<=620);assert.equal(motion.duration(10000,true),0);
  console.log('PASS: smooth intermediate frames, bounded duration, pane clamping, cancellation without late writes, reduced motion, nearby vs distant spotlight');
})().catch(e=>{console.error(e);process.exitCode=1;});
