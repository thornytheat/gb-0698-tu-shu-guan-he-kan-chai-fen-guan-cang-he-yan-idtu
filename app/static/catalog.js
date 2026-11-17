const state=JSON.parse(document.getElementById('catalog-state').textContent);
const editor=document.getElementById('content');
const message=document.getElementById('message');
const amendMessage=document.getElementById('amend-message');

document.getElementById('file').addEventListener('change',async function(){
  if(!this.files[0])return;
  const data=await this.files[0].arrayBuffer();
  let text;
  try{text=new TextDecoder('utf-8',{fatal:true}).decode(data)}
  catch{text=new TextDecoder('gb18030').decode(data)}
  editor.value=text;
});
document.getElementById('demo').onclick=async()=>{
  try{editor.value=(await(await fetch('/api/sample')).json()).text;message.textContent='馆藏样例已载入'}
  catch{message.textContent='读取失败，请重试'}
};

async function commit(url,data){
  const reply=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const body=await reply.json();
  if(!reply.ok)throw new Error(body.message||'核验失败，请检查记录');
  return body;
}

document.getElementById('catalog-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const submitter=e.submitter;
  submitter.disabled=true;
  message.textContent='正在展开卷期…';
  try{const batch=await commit('/api/run',{text:editor.value});location.assign(location.pathname+'?run_id='+batch.id)}
  catch(err){message.textContent=err.message;submitter.disabled=false}
});

let confirmedRevision=state.revision;
document.querySelectorAll('[data-action]').forEach(button=>button.onclick=async()=>{
  const action=button.dataset.action;
  try{
    // 复核只会把最后确认的快照追加为留痕，失败时页面继续显示上一次已签复核。
    const updated=await commit('/api/review',{id:state.id,revision:confirmedRevision,decision:action});
    confirmedRevision=updated.revision;
    message.textContent=updated.decision+' · 版本 '+updated.revision
      +(updated.confirmed_current?' · 已对齐当前装订事实':' · 当前批次切换失败，仍显示原复核');
    location.reload();
  }catch(err){message.textContent=err.message}
});

const amendForm=document.getElementById('amend-form');
if(amendForm){
  document.getElementById('amend-demo').onclick=()=>{
    document.getElementById('amend-records').value='00001';
    document.getElementById('amend-segments').value=JSON.stringify([
      {issues:'1',binding:'B01A',record_id:'00001'},
      {issues:'2',binding:'B01B',record_id:'00001-S2'}
    ],null,0);
    amendMessage.textContent='示例：把 1-2 合刊拆为上下两册，修订号 '+(state.amendment_revision+1);
  };
  amendForm.addEventListener('submit',async e=>{
    e.preventDefault();
    const revision=Number.parseInt(document.getElementById('amend-revision').value,10);
    const recordText=document.getElementById('amend-records').value.trim();
    let segments;
    try{segments=JSON.parse(document.getElementById('amend-segments').value)}
    catch{amendMessage.textContent='装订分段不是合法 JSON';return}
    const change={segments};
    if(recordText)change.record_ids=recordText.split(/[,，\s]+/).filter(Boolean);
    else amendMessage.textContent='请填写修订目标记录号';
    if(!recordText)return;
    amendMessage.textContent='正在重放修订…';
    try{
      const reply=await commit('/api/amend',{id:state.id,revision,changes:[change],
        note:document.getElementById('amend-note').value.trim()});
      amendMessage.textContent=(reply.status==='replayed'?'重办同一修订，未生成新版本':'修订 '+reply.revision+' 已生效')+' · 实体册 '+reply.run.books;
      location.reload();
    }catch(err){amendMessage.textContent=err.message}
  });
}
