"""Small Record integration; full library lives on its own local-only page."""

SOP_CARD_HTML = r'''
  <div id="sopSavedChoice" hidden><p style="margin-top:14px">要把这次作业整理成 SOP 吗？</p>
    <div class="file-actions"><button id="sopGenerate">生成 SOP</button><button id="sopLater">稍后整理</button><button id="sopKeep">仅保留录像</button></div>
  </div>
  <p style="margin:12px 0 0"><a href="/library" id="sopSavedLink" target="_blank" rel="noopener" hidden>查看录像与 SOP →</a></p>
'''

SOP_DIALOG_HTML = r'''
<dialog id="sopGenerateDialog" aria-labelledby="sopDialogTitle">
  <div class="sop-dialog-heading"><h2 id="sopDialogTitle">生成 SOP</h2><button id="sopDialogClose" type="button" aria-label="关闭生成 SOP">×</button></div>
  <p>Antigravity 后台整理，完成后再核对。</p>
  <label>作业名称<input id="sopTaskName" maxlength="160" placeholder="例如：配置检查与保存"></label>
  <label>作业日期<input id="sopTaskDate" type="date"></label>
  <details><summary>更多信息 · 选填</summary><label>环境<input id="sopTaskEnvironment" maxlength="120" placeholder="待确认"></label>
    <label>整理方式<select id="sopExisting"><option value="">新建 SOP</option></select></label>
  </details><p id="sopDialogError" role="alert"></p>
  <div class="file-actions" style="justify-content:flex-end"><button id="sopDialogCancel" type="button">返回</button><button id="sopDialogStart" type="button">开始整理</button></div>
</dialog>
'''

SOP_STYLE = r'''
<style>
.record-panel{width:min(320px,calc(100vw - 24px))}
#recordAudioAdvanced>summary{font-size:12px;color:#b7c6dc;padding:8px 0;cursor:pointer}
#recordAudioAdvanced .rec-channel:first-of-type{border-top:0;margin-top:2px}
#recordAudioAdvanced{margin-top:9px}
#sopLibraryLink{display:block;border-top:1px solid #384b66;padding:13px 0 0;margin-top:12px;color:#dce8f8;text-decoration:none;font:13px system-ui,sans-serif}
#recordingResult{top:auto;bottom:16px}
#sopGenerateDialog{width:410px;max-width:calc(100vw - 24px);max-height:calc(100dvh - 30px);overflow:auto;padding:22px;background:#18263b;color:#e0e9f7;border:1px solid #45617d;border-radius:10px;font:13px system-ui,sans-serif}
#sopGenerateDialog::backdrop{background:#0004}
#sopGenerateDialog label{display:grid;gap:7px;margin-top:16px;color:#b7c6dc;font-size:12px}
#sopGenerateDialog input,#sopGenerateDialog select{min-width:0;width:100%;padding:8px 10px;border:1px solid #45617d;background:#162238;color:#e0e9f7;border-radius:5px;font:14px system-ui,sans-serif}
#sopGenerateDialog details{margin:18px 0;font-size:12px;color:#b7c6dc}
#sopGenerateDialog p{color:#b7c6dc;font-size:12px;line-height:1.6}
.sop-dialog-heading{display:flex;align-items:center;justify-content:space-between}
.sop-dialog-heading h2{font-size:18px;font-weight:500;margin:0}
#sopDialogClose{border:0;background:none;color:#b7c6dc;font-size:22px;cursor:pointer}
#sopGenerate,#sopDialogStart{background:#255fae;border-color:#4c83c8}
#sopKeep{background:transparent;border-color:transparent;color:#b7c6dc}
#sopSavedLink{color:#a5c8fa;font-size:12px}
#sopDialogError{color:#ffc0a6!important}
.recording-more{font-size:12px;color:#b7c6dc;align-self:center}.recording-more>summary{cursor:pointer}.recording-more .file-actions{padding-top:8px}
</style>
'''

SOP_VIEWER_JS = r'''
// SOP documents and controls never share the target input socket.
let sopSocket=null,sopReady=null,sopCounter=0,sopEnabled=false,sopRecords=[],sopCurrent=null;
const sopRequests=new Map();
function sopCall(action,args={}) {
  if(!sopReady){
    sopReady=new Promise((resolve,reject)=>{
      sopSocket=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/sop/ws');
      const connectTimer=setTimeout(()=>reject(Error('资料库连接超时')),15000);
      sopSocket.onmessage=e=>{try{const msg=JSON.parse(e.data);
        if(msg.type==='auth_required')sopSocket.send(JSON.stringify({type:'auth',token:storedToken()}));
        else if(msg.type==='auth_failed')reject(Error('请重新连接录屏服务后打开资料库'));
        else if(msg.type==='ready'){clearTimeout(connectTimer);resolve();}
        else if(msg.type==='reply'){const entry=sopRequests.get(msg.request);if(entry){clearTimeout(entry.timer);sopRequests.delete(msg.request);msg.error?entry.reject(Error(msg.error)):entry.resolve(msg.result);}}
      }catch(e){reject(e);}};
      sopSocket.onclose=()=>{clearTimeout(connectTimer);sopReady=null;reject(Error('资料库连接中断'));for(const entry of sopRequests.values()){clearTimeout(entry.timer);entry.reject(Error('资料库连接中断，请刷新确认结果'));}sopRequests.clear();};
    });
  }
  return sopReady.then(()=>new Promise((resolve,reject)=>{const request=++sopCounter;const timer=setTimeout(()=>{sopRequests.delete(request);reject(Error('请求超时，请刷新确认状态'));},60000);sopRequests.set(request,{resolve,reject,timer});sopSocket.send(JSON.stringify({action,request,...args}));}));
}
const originalRecordingActions=recordingFileActions;
recordingFileActions=function(item){
  const original=originalRecordingActions(item),actions=document.createElement('div');actions.className='file-actions';
  actions.append(original.firstChild);
  const more=document.createElement('details');more.className='recording-more';const summary=document.createElement('summary');summary.textContent='更多';more.append(summary,original);actions.append(more);return actions;
};
const baseRenderRecordingLibrary=renderRecordingLibrary;
renderRecordingLibrary=function(msg){
  sopEnabled=!!msg.sop_enabled;
  document.getElementById('legacyRecordingHeading').hidden=sopEnabled;
  document.getElementById('recentRecordings').hidden=sopEnabled;
  document.getElementById('sopLibraryLink').hidden=!sopEnabled;
  document.getElementById('sopSavedLink').hidden=!sopEnabled;
  if(msg.sop_prompt){resultRecordingId=msg.sop_prompt;resultRecordingPending=true;}
  baseRenderRecordingLibrary(msg);
  const item=msg.items.find(x=>x.id===resultRecordingId);
  if(item){
    const date=item.recorded_start_at?new Date(item.recorded_start_at).toLocaleString('sv-SE',{timeZone:'Asia/Tokyo'}):null;
    document.getElementById('recordingResultName').textContent=date?date+' JST':item.name;
    document.getElementById('sopSavedChoice').hidden=!(sopEnabled&&(!item.sop_choice||item.sop_choice==='pending'));
  }
};
async function showSopGenerate(){
  if(!resultRecordingId)return;
  try{
    const snapshot=await sopCall('snapshot');sopRecords=snapshot.recordings;sopCurrent=sopRecords.find(x=>x.id===resultRecordingId);
    if(!sopCurrent)throw Error('找不到录像，请刷新资料库');
    document.getElementById('sopTaskName').value=sopCurrent.title==='未命名录像'?'':sopCurrent.title;
    document.getElementById('sopTaskDate').value=sopCurrent.operation_date||'';
    document.getElementById('sopTaskEnvironment').value=sopCurrent.environment||'';
    const select=document.getElementById('sopExisting');select.replaceChildren(new Option('新建 SOP',''));
    for(const sop of snapshot.sops.filter(x=>x.active))select.add(new Option('更新：'+sop.title+' · v'+sop.number,sop.id));
    document.getElementById('sopDialogError').textContent='';document.getElementById('sopGenerateDialog').showModal();
  }catch(error){showHint(error.message);}
}
document.getElementById('sopGenerate').onclick=showSopGenerate;
for(const [id,choice] of [['sopLater','later'],['sopKeep','keep']])document.getElementById(id).onclick=async()=>{
  try{await sopCall('choice',{id:resultRecordingId,choice});document.getElementById('sopSavedChoice').hidden=true;showHint(choice==='later'?'已加入待整理':'仅保留录像，未开始分析');refreshRecordingLibrary();}catch(e){showHint(e.message);}
};
for(const id of ['sopDialogClose','sopDialogCancel'])document.getElementById(id).onclick=()=>document.getElementById('sopGenerateDialog').close();
document.getElementById('sopDialogStart').onclick=async()=>{
  const button=document.getElementById('sopDialogStart');button.disabled=true;
  try{const date=document.getElementById('sopTaskDate').value;if(!date)throw Error('请确认业务作业日期');
    await sopCall('enqueue',{id:sopCurrent.id,title:document.getElementById('sopTaskName').value||'未命名作业',date,environment:document.getElementById('sopTaskEnvironment').value,sop_id:document.getElementById('sopExisting').value||null});
    document.getElementById('sopGenerateDialog').close();document.getElementById('sopSavedChoice').hidden=true;showHint('已加入 Antigravity 整理队列；在录像与 SOP 中查看进度');refreshRecordingLibrary();
  }catch(error){document.getElementById('sopDialogError').textContent=error.message;}finally{button.disabled=false;}
};
'''


def install_sop_viewer(html):
    html=html.replace('</head>',SOP_STYLE+'</head>')
    html=html.replace('<section class="rec-channel" id="recMicChannel"','<details id="recordAudioAdvanced"><summary>设备与音量</summary><section class="rec-channel" id="recMicChannel"',1)
    html=html.replace('<p id="recAudioStatus" role="status"></p>','<p id="recAudioStatus" role="status"></p></details><a id="sopLibraryLink" href="/library" target="_blank" rel="noopener" hidden>录像与 SOP ↗</a>',1)
    html=html.replace('<h3>Recent recordings <button','<h3 id="legacyRecordingHeading" hidden>Recent recordings <button',1)
    html=html.replace('<div id="recentRecordings"','<div hidden id="recentRecordings"',1)
    html=html.replace('<div id="recordingResultActions" class="file-actions"></div>','<div id="recordingResultActions" class="file-actions"></div>'+SOP_CARD_HTML,1)
    html=html.replace('<div id="hint"></div>',SOP_DIALOG_HTML+'<div id="hint"></div>',1)
    # Nested audio disclosure must not close the outer toolbar menu.
    html=html.replace('toolbar.querySelectorAll("details")','toolbar.querySelectorAll(".tool-group")')
    html=html.replace('toolbar.querySelectorAll("details[open]")','toolbar.querySelectorAll(".tool-group[open]")')
    html=html.replace('body:has(#toolbar details[open])','body:has(#toolbar .tool-group[open])')
    html=html.replace('Recording settings and recent files','Recording settings')
    html=html.replace('</script>',SOP_VIEWER_JS+'\n</script>',1)
    return html
