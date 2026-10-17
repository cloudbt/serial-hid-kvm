"""Observable ledger, publication, isolation, and transport invariants."""
import asyncio
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
import websockets

from serial_hid_kvm._recording_library import RecordingLibrary
from serial_hid_kvm._recording_session import RecordingSessions
from serial_hid_kvm._sop_store import SopStore, atomic_text
from serial_hid_kvm._sop_store import display_steps
from serial_hid_kvm._sop_agy import SopWorker, validate_result, markdown_result
from serial_hid_kvm._sop_guard import decide
from serial_hid_kvm._sop_http import local_request
from tests.test_web_auth import _make_server, _recv_json


@pytest.fixture
def library(tmp_path):
    folder=tmp_path/'recordings'
    folder.mkdir()
    filename='recording-2026-10-15T17-32-51-831-JST-64e7b427.mp4'
    (folder/filename).write_bytes(b'fake-mp4')
    store=SopStore(tmp_path,RecordingLibrary(folder))
    store.sync()
    return store,store.snapshot()['recordings'][0]['id']


def result():
    return dict(title='示例流程',goal='检查目标',prerequisites=['已确认目标'],inputs=['环境'],
                steps=[dict(at=2,action='核对对象',expected='正确对象',verify='画面中的编号',stop='编号不符')],
                uncertainties=['核对环境'],coverage='观察到测试画面，无音轨证据')


def review_job(store,key,sop_id=None):
    job=store.enqueue(key,sop_id)
    data=result()
    store.update_job(job,status='review',data=json.dumps(data),draft=markdown_result(data,store.get_recording(key)))
    return job


def test_import_dates_choice_prompt_and_missing_file(library):
    store,key=library
    row=store.get_recording(key)
    assert row['operation_date']=='2026-10-15'
    assert row['media']['recorded_start_at']=='2026-10-15T17:32:51.831000+09:00'
    assert store.pending_prompt() is None # importing old media does not pop up
    store.notice_saved(key)
    assert store.pending_prompt()['id']==key
    assert store.pending_prompt() is None
    store.notice_saved(key)
    assert store.pending_prompt() is None # duplicate finish never reopens it
    store.edit_recording(key,'配置检查','2026-10-14','TEST')
    store.choose(key,'keep')
    store.sync()
    row=store.get_recording(key)
    assert row['operation_date']=='2026-10-14' and row['choice']=='keep'
    assert row['media']['recorded_start_at'].startswith('2026-10-15')
    assert store.snapshot()['recordings'][0]['job'] is None
    (store.recordings.root/row['media']['path']).unlink()
    store.sync()
    assert store.get_recording(key)['media']['missing']
    with pytest.raises(ValueError):store.enqueue(key)


def test_source_to_mp4_is_one_recording_and_keeps_duration(tmp_path):
    recordings=tmp_path/'recordings';sessions=RecordingSessions(recordings);session='a'*32
    sessions.attach(session,'recording-2026-10-16T08-36-34-532-JST.webm')
    sessions.write(session,0,b'webm')
    source,mp4=sessions.finish(session,1,dict(recorded_end_at='2026-10-16T00:00:00Z',duration_seconds=100))
    store=SopStore(tmp_path,RecordingLibrary(recordings));store.sync()
    first=store.snapshot()['recordings'][0]
    assert first['media']['duration_seconds']==100
    store.choose(first['id'],'later');mp4.write_bytes(b'mp4');store.sync()
    final=store.snapshot()['recordings']
    assert len(final)==1 and final[0]['id']==first['id']
    assert final[0]['media']['path']==mp4.name and final[0]['choice']=='later'
    store.media_facts(first['id'],99,'digest');store.sync()
    assert store.get_recording(first['id'])['media']['probed_duration']==99


def test_queue_idempotence_and_worker_lease(library):
    store,key=library
    job=store.enqueue(key)
    assert store.enqueue(key)==job
    assert store.claim('first')['id']==job
    assert store.claim('second') is None
    store.cancel(job)
    assert store.job(job)['status']=='canceled'
    store.update_job(job,status='review')
    assert store.job(job)['status']=='canceled' # late result cannot resurrect it
    store.retry(job)
    assert store.job(job)['status']=='queued'


def test_publish_requires_review_pins_skill_and_preserves_versions(library):
    store,key=library
    job=review_job(store,key)
    assert not list((store.project/'.agents').glob('**/SKILL.md'))
    with pytest.raises(ValueError):store.publish(job,'Tester')
    store.save_draft(job,store.job(job)['draft'],'TEST',{})
    with pytest.raises(ValueError):store.publish(job,'Tester')
    store.save_draft(job,store.job(job)['draft'],'TEST',{'0':'核对视频和申请信息，TEST'})
    v1=store.publish(job,'Tester')
    assert store.publish(job,'Tester')==v1
    detail=store.detail(version_id=v1);sop=detail['version']['sop_id']
    assert '状态：已人工核对，已发布' in detail['markdown']
    old=(store.root/sop/'versions/v0001.md').read_text(encoding='utf-8')
    skill=store.project/'.agents/skills'/('targetpc2-'+sop)/'SKILL.md'
    assert 'versions/v0001.md' in skill.read_text(encoding='utf-8')
    next_job=review_job(store,key,sop)
    store.save_draft(next_job,store.job(next_job)['draft']+'\n修订内容\n','TEST',{'0':'确认 TEST'})
    v2=store.publish(next_job,'Tester')
    assert v2!=v1 and store.detail(version_id=v2)['version']['number']==2
    assert (store.root/sop/'versions/v0001.md').read_text(encoding='utf-8')==old
    assert 'versions/v0002.md' in skill.read_text(encoding='utf-8')
    assert len(store.get_recording(key)['versions'])==2
    store.withdraw(sop)
    assert not skill.exists()
    assert (store.root/sop/'versions/v0001.md').exists()
    assert (store.local/'backups'/store.backup()).is_file()


def test_staged_publication_recovers_after_filesystem_failure(library,monkeypatch):
    store,key=library;job=review_job(store,key)
    store.save_draft(job,store.job(job)['draft'],'TEST',{'0':'已核对'})
    import serial_hid_kvm._sop_store as module
    real=module.atomic_text
    def fail_skill(path,text):
        if Path(path).name=='SKILL.md':raise OSError('simulated crash')
        real(path,text)
    monkeypatch.setattr(module,'atomic_text',fail_skill)
    with pytest.raises(OSError):store.publish(job,'Tester')
    monkeypatch.setattr(module,'atomic_text',real)
    restored=SopStore(store.project,store.recordings)
    assert restored.job(job)['status']=='published'
    assert len(restored.get_recording(key)['versions'])==1


def test_guard_exact_read_allowlist_and_all_other_tools_denied(tmp_path):
    allowed=tmp_path/'clip.mp4';allowed.write_bytes(b'data')
    paths={str(allowed.resolve())}
    def call(name,args):return {'toolCall':{'name':name,'args':args}}
    assert decide(call('view_file',{'AbsolutePath':str(allowed)}),paths)=='auto_approve'
    assert decide(call('finish',{}),paths)=='allow'
    for name in ('run_command','call_mcp_tool','write_to_file','read_url_content','invoke_subagent','ask_permission'):
        assert decide(call(name,{}),paths)=='deny'
    assert decide(call('view_file',{'AbsolutePath':str(tmp_path/'other.mp4')}),paths)=='deny'
    with pytest.raises(ValueError):validate_result({**result(),'steps':[{**result()['steps'][0],'at':100}]},3)


def test_human_markdown_edits_are_displayed_and_existing_docs_are_readable(library):
    store,key=library
    md=markdown_result(result(),store.get_recording(key))
    changed=md.replace('| 1 | 00:02 | 核对对象 |','| 1 | 00:03 | 检查新对象 |')
    steps=display_steps(changed)
    assert steps[0]['at']==3 and steps[0]['action']=='检查新对象'
    job=review_job(store,key)
    store.save_draft(job,changed,'TEST',{'0':'核对完成'})
    assert store.detail(job_id=job)['display_steps']==steps
    atomic_text(store.root/'legacy-procedure.md','# 已有手顺\n\n保留原文。\n')
    docs=store.snapshot()['documents']
    assert len(docs)==1 and store.document(docs[0]['id'])['markdown'].startswith('# 已有手顺')
    assert not list((store.project/'.agents').glob('**/SKILL.md'))


def test_worker_resumes_successful_segment_without_model_call(library,monkeypatch):
    store,key=library
    worker=SopWorker(store,shutil.which('python') or 'python',segment_seconds=120)
    data=result()
    import serial_hid_kvm._sop_agy as module
    calls=[]
    class FakeProcess:
        returncode=0;pid=999999
        def __init__(self,args,**kw):
            Path(args[-1]).write_bytes(b'clip');calls.append('ffmpeg')
        def poll(self):return 0
        def wait(self,**kw):return 0
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout='{"format":{"duration":"4"},"streams":[]}'))
    monkeypatch.setattr(module.subprocess,'Popen',FakeProcess)
    monkeypatch.setattr(module,'run_agy',lambda *a,**k:(calls.append('agy') or data))
    job=store.enqueue(key);claimed=store.claim(worker.owner);worker.analyze(claimed)
    assert store.job(job)['status']=='review'
    # Retrying the exact same immutable recipe uses its successful segment.
    store.update_job(job,status='failed');store.retry(job);worker.analyze(store.claim(worker.owner))
    assert calls==['ffmpeg','agy']
    assert store.job(job)['done']==1


def test_local_transport_no_origin_or_dns_rebinding_and_no_target_access(tmp_path):
    async def run():
        folder=tmp_path/'recordings';folder.mkdir();(folder/'recording-2026-10-16T01-00-00-000-JST.mp4').write_bytes(b'123')
        server,url=_make_server(recording_dir=str(folder),sop_project_dir=str(tmp_path))
        server._sop.worker=None # transport tests never start a real AI job
        await server.start();base=url.replace('ws:','http:').removesuffix('/ws');sop_url=url.removesuffix('/ws')+'/sop/ws'
        try:
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(sop_url):pass
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(sop_url,origin='http://evil.example'):pass
            async with websockets.connect(sop_url,origin=base) as ws:
                assert (await _recv_json(ws))['type']=='ready'
                await ws.send(json.dumps({'action':'snapshot','request':1}))
                reply=await _recv_json(ws);assert len(reply['result']['recordings'])==1
                await ws.send(json.dumps({'action':'keydown','request':2,'code':'Enter'}))
                assert 'error' in await _recv_json(ws)
            def page(host=None):
                with urlopen(Request(base+'/library',headers={'Host':host} if host else {})) as r:return r.read()
            assert '录像与 SOP' in (await asyncio.to_thread(page)).decode()
            with pytest.raises(HTTPError) as blocked:await asyncio.to_thread(page,'evil.example')
            assert blocked.value.code==403
        finally:await server.stop()
    asyncio.run(run())
