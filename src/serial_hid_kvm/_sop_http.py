"""Local-only SOP transport, intentionally separate from video/HID sockets."""
import asyncio
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

import websockets
from websockets.http11 import Response

from ._sop_store import SopStore, confined
from ._sop_agy import SopWorker

logger=logging.getLogger(__name__)


def local_request(connection,request,origin=False):
    try:
        if not ipaddress.ip_address(connection.remote_address[0]).is_loopback:
            return False
        host=request.headers.get('Host','')
        target=urlsplit('http://'+host)
        if target.hostname not in ('localhost','127.0.0.1','::1'):
            return False
        if origin:
            source=urlsplit(request.headers.get('Origin',''))
            if source.scheme not in ('http','https') or source.netloc!=host:
                return False
        return True
    except (AttributeError,TypeError,ValueError,IndexError):
        return False


class SopService:
    def __init__(self,viewer):
        self.viewer=viewer
        config=viewer._config
        project=getattr(config,'sop_project_dir','')
        guess=Path(config.recording_dir).expanduser().resolve().parent
        self.store=None
        self.worker=None
        if project=='off' or (not project and not (guess/'knowledge'/'sops').is_dir()):
            return
        try:
            self.store=SopStore(project or guess,viewer._recording_library)
            self.worker=SopWorker(self.store,getattr(config,'sop_agy_executable',''),getattr(config,'sop_segment_seconds',120),getattr(config,'sop_agy_timeout',1200))
        except Exception:
            logger.exception('SOP library unavailable; recording remains enabled')
            self.store=None
            self.worker=None

    def start(self):
        if self.worker:
            try:
                self.store.sync()
                self.worker.start()
            except Exception:
                logger.exception('SOP startup failed; recording remains enabled')
                self.store=None
                self.worker=None

    def stop(self):
        if self.worker:
            self.worker.stop()

    def page(self,connection,request):
        if not self.store or not local_request(connection,request):
            return Response(403,'Forbidden',websockets.Headers({'Content-Type':'text/plain; charset=utf-8'}),'资料库仅在配置的 Host 本机开放。'.encode())
        from ._sop_page import SOP_PAGE
        body=SOP_PAGE.encode('utf-8')
        return Response(200,'OK',websockets.Headers({'Content-Type':'text/html; charset=utf-8','Content-Length':str(len(body)),
             'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
             'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}),body)

    async def socket(self,ws):
        if not self.store or not local_request(ws,ws.request,origin=True):
            await ws.close(1008,'Local same-origin clients only')
            return
        if self.viewer._config.web_password:
            if not await self.viewer._authenticate(ws,ws.remote_address[0]):
                await ws.close(1008,'Authentication required')
                return
        await ws.send(json.dumps({'type':'ready'}))
        async for message in ws:
            request={}
            try:
                if not isinstance(message,str) or len(message)>300000:
                    raise ValueError('请求过大或格式错误')
                request=json.loads(message)
                if not isinstance(request,dict):
                    raise ValueError('请求格式错误')
                result=await self.dispatch(ws,request)
                reply={'type':'reply','request':request.get('request'),'result':result}
            except (OSError,ValueError,KeyError,TypeError) as exc:
                reply={'type':'reply','request':request.get('request') if isinstance(request,dict) else None,'error':str(exc)}
            except Exception:
                reply={'type':'reply','request':request.get('request'),'error':'处理失败，资料保留；请查看 Host 服务日志'}
            await ws.send(json.dumps(reply,ensure_ascii=False))

    async def dispatch(self,ws,request):
        action=request.get('action')
        store=self.store
        def call(fn,*args):
            return asyncio.to_thread(fn,*args)
        if action=='snapshot':
            await call(store.sync)
            snapshot=await call(store.snapshot)
            snapshot['target']=self.viewer._config.target_name or store.project.name
            return snapshot
        if action=='prompt':
            await call(store.sync)
            return await call(store.pending_prompt,request.get('id'))
        if action=='edit':
            return await call(store.edit_recording,request['id'],request['title'],request['date'],request.get('environment',''))
        if action=='choice':
            return await call(store.choose,request['id'],request['choice'])
        if action=='enqueue':
            await call(store.edit_recording,request['id'],request['title'],request['date'],request.get('environment',''))
            return await call(store.enqueue,request['id'],request.get('sop_id'))
        if action=='detail':
            detail=await asyncio.to_thread(store.detail,request.get('job_id'),request.get('version_id'))
            media=detail['recording']['media']
            if not media.get('missing'):
                detail['url']=store.recordings.file_url(media['path'])
            return detail
        if action=='document':
            return await call(store.document,request['id'])
        if action=='save_draft':
            return await call(store.save_draft,request['job_id'],request['markdown'],request['environment'],request.get('resolutions',{}))
        if action=='publish':
            if request.get('reviewed') is not True:
                raise ValueError('需要明确核对通过后发布')
            return await call(store.publish,request['job_id'],request['reviewer'])
        if action=='cancel':
            return await call(store.cancel,request['job_id'])
        if action=='retry':
            return await call(store.retry,request['job_id'])
        if action=='open':
            return await call(store.recordings.open_local,request['id'],request.get('mode','open'))
        if action=='open_sop':
            if os.name!='nt':raise ValueError('系统打开仅支持 Windows Host')
            with store.connect() as db:
                row=db.execute("SELECT v.*,s.slug FROM versions v JOIN sops s ON v.sop_id=s.id WHERE v.id=? AND v.status='published'",(request['version_id'],)).fetchone()
            if not row:raise ValueError('找不到已发布 SOP')
            path=confined(store.root,store.root/row['slug']/'versions'/f"v{row['number']:04d}.md")
            await call(os.startfile,str(path))
            return '已在本机打开 SOP'
        if action=='withdraw':
            if request.get('confirmed') is not True:raise ValueError('请确认停用范围')
            return await call(store.withdraw,request['sop_id'])
        if action=='backup':
            return await call(store.backup)
        if action=='retry_export':
            row=store.get_recording(request['id'])
            media=row['media']
            source=confined(store.recordings.root,store.recordings.root/media.get('source',media['path']))
            if source.suffix.lower()!='.webm' or not source.is_file():raise ValueError('找不到原始 WebM')
            if row['session_id']:
                manifest=json.loads(source.with_name('session.json').read_text(encoding='utf-8'))
                destination=confined(store.recordings.root,store.recordings.root/manifest['filename'])
            else:
                destination=source.with_suffix('.mp4')
            if destination.exists():raise ValueError('MP4 已存在，请刷新；没有覆盖')
            key=row['session_id'] or row['id']
            pending=self.viewer._session_exports.get(key)
            if not pending or pending.done():
                self.viewer._session_exports[key]=self.viewer._export_recording(source,ws,destination,key)
            return '已加入转换队列'
        raise ValueError('未知资料库操作')
