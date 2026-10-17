"""Local recording/SOP ledger. No model or target control runs in this module."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

JST = timezone(timedelta(hours=9))
ACTIVE_JOBS = ("queued", "running", "waiting_media")


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def atomic_text(path, text):
    path = Path(path)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def confined(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("文件超出允许目录")
    return path


def text_value(value, limit=300):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError("文字内容无效或过长")
    return value.strip()


def recording_time(name):
    match = re.search(r"recording-(\d{4}-\d\d-\d\d)T(\d\d)-(\d\d)-(\d\d)-(\d{3})-JST", name)
    if not match:
        return None
    try:
        return datetime.fromisoformat(f"{match[1]}T{match[2]}:{match[3]}:{match[4]}.{match[5]}+09:00")
    except ValueError:
        return None


def display_steps(markdown):
    """Read the human-edited canonical step table, never show stale AI steps."""
    result = []
    for line in markdown.splitlines():
        parts = [p.strip().replace('\\|','|') for p in re.split(r'(?<!\\)\|',line.strip().strip('|'))]
        if len(parts) != 6 or not parts[0].isdigit():
            continue
        match = re.fullmatch(r'(\d+):(\d\d)(?::(\d\d))?',parts[1])
        if not match:
            continue
        at = int(match[1])*60+int(match[2]) if match[3] is None else int(match[1])*3600+int(match[2])*60+int(match[3])
        result.append(dict(at=at,action=parts[2],expected=parts[3],verify=parts[4],stop=parts[5]))
    return result


class SopStore:
    def __init__(self, project, recordings):
        self.project = Path(project).resolve()
        self.recordings = recordings
        self.root = self.project / "knowledge" / "sops"
        self.local = self.project / ".local" / "sop-library"
        self.local.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.local / "library.sqlite"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS recordings(
                id TEXT PRIMARY KEY, session_id TEXT, title TEXT NOT NULL,
                operation_date TEXT, environment TEXT DEFAULT '', choice TEXT DEFAULT 'pending',
                prompt_seen INTEGER DEFAULT 0, media TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sops(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
                current_version TEXT, active INTEGER DEFAULT 1);
              CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, recording_id TEXT NOT NULL, sop_id TEXT,
                status TEXT NOT NULL, done INTEGER DEFAULT 0, total INTEGER DEFAULT 0,
                message TEXT DEFAULT '', updated_at TEXT NOT NULL, owner TEXT,
                data TEXT, draft TEXT, resolutions TEXT DEFAULT '{}', error TEXT DEFAULT '');
              CREATE UNIQUE INDEX IF NOT EXISTS one_active_recording_job ON jobs(recording_id)
                WHERE status IN ('queued','running','waiting_media');
              CREATE TABLE IF NOT EXISTS versions(
                id TEXT PRIMARY KEY, sop_id TEXT NOT NULL, job_id TEXT UNIQUE NOT NULL,
                number INTEGER NOT NULL, markdown TEXT NOT NULL, status TEXT NOT NULL,
                reviewed_at TEXT NOT NULL, reviewer TEXT NOT NULL, sha256 TEXT NOT NULL,
                UNIQUE(sop_id, number));
              CREATE TABLE IF NOT EXISTS links(
                recording_id TEXT NOT NULL, version_id TEXT NOT NULL,
                PRIMARY KEY(recording_id,version_id));
              CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, subject TEXT NOT NULL,
                action TEXT NOT NULL, detail TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS worker_lock(id INTEGER PRIMARY KEY, owner TEXT, expires REAL);
            """)
        self.recover_publications()

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.db, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def event(db, subject, action, detail=""):
        db.execute("INSERT INTO events(at,subject,action,detail) VALUES(?,?,?,?)",
                   (utcnow(), subject, action, detail))

    def sync(self):
        catalog = self.recordings.catalog()
        with self.connect() as db:
            for key, item in catalog.items():
                path = item["path"]
                start = recording_time(item["name"])
                manifest = None
                session = item.get("session_id")
                if session:
                    try:
                        manifest = json.loads((self.recordings.root / ".recording-sessions" / session / "session.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
                media = dict(name=item["name"], path=path.relative_to(self.recordings.root).as_posix(),
                             state=item["state"], size=item["size"], modified=item["modified"],
                             recorded_start_at=start.isoformat() if start else None,
                             date_source="filename_jst" if start else "unknown", missing=False)
                if manifest:
                    for field in ("recorded_end_at", "duration_seconds"):
                        media[field] = manifest.get(field)
                    media["source"] = f".recording-sessions/{session}/source.webm"
                payload = json.dumps(media, ensure_ascii=False)
                prior = db.execute("SELECT media FROM recordings WHERE id=?", (key,)).fetchone()
                if prior:
                    old = json.loads(prior['media'])
                    if all(old.get(field) == media.get(field) for field in ('path','size','modified')):
                        for field in ('probed_duration','sha256'):
                            if field in old:
                                media[field] = old[field]
                        payload = json.dumps(media, ensure_ascii=False)
                if prior and prior["media"] == payload:
                    continue
                db.execute("""INSERT INTO recordings(id,session_id,title,operation_date,media,updated_at,prompt_seen)
                    VALUES(?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET
                    session_id=COALESCE(excluded.session_id,recordings.session_id),
                    media=excluded.media,updated_at=excluded.updated_at""",
                    (key, session, "未命名录像", start.date().isoformat() if start else None, payload, utcnow()))
            for row in db.execute("SELECT id,media FROM recordings").fetchall():
                if row["id"] not in catalog:
                    media = json.loads(row["media"])
                    if not media.get("missing"):
                        media["missing"] = True
                        db.execute("UPDATE recordings SET media=?,updated_at=? WHERE id=?",
                                   (json.dumps(media, ensure_ascii=False), utcnow(), row["id"]))
        self.export_index()

    def notice_saved(self, key):
        self.sync()
        with self.connect() as db:
            if not db.execute("SELECT id FROM events WHERE subject=? AND action='recording_saved'", (key,)).fetchone():
                db.execute("UPDATE recordings SET prompt_seen=0 WHERE id=? AND choice='pending'", (key,))
                self.event(db,key,"recording_saved")

    def snapshot(self):
        with self.connect() as db:
            records = []
            for row in db.execute("SELECT * FROM recordings ORDER BY operation_date DESC, json_extract(media,'$.recorded_start_at') DESC"):
                item = dict(row)
                item["media"] = json.loads(item["media"])
                job = db.execute("SELECT id,status,done,total,message,error,sop_id,updated_at FROM jobs WHERE recording_id=? ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()
                item["job"] = dict(job) if job else None
                item["versions"] = [dict(v) for v in db.execute("""SELECT v.id,v.sop_id,v.number,v.reviewed_at,s.title,s.active
                    FROM links l JOIN versions v ON l.version_id=v.id JOIN sops s ON v.sop_id=s.id
                    WHERE l.recording_id=? AND v.status='published' ORDER BY v.number DESC""", (row["id"],))]
                records.append(item)
            sops = [dict(row) for row in db.execute("""SELECT s.*,v.number,v.reviewed_at,
                    (SELECT count(DISTINCT recording_id) FROM links JOIN versions vv ON version_id=vv.id WHERE vv.sop_id=s.id) AS recordings
                    FROM sops s LEFT JOIN versions v ON s.current_version=v.id ORDER BY v.reviewed_at DESC""")]
        # Only IDs / relative paths are exposed; signed URLs are freshly issued.
        for item in records:
            if not item["media"].get("missing"):
                item["url"] = self.recordings.file_url(item["media"]["path"])
        return {"recordings": records, "sops": sops, "documents": self.documents()}

    def documents(self):
        """Existing SOPs are readable without inventing publication/validation."""
        with self.connect() as db:
            managed = {row[0] for row in db.execute('SELECT slug FROM sops')}
            managed.update('sop-'+row[0][:12] for row in db.execute('SELECT id FROM jobs'))
        result=[]
        for path in sorted(self.root.rglob('*.md')):
            relative=path.relative_to(self.root)
            if path.name.lower()=='readme.md' or relative.parts[0] in managed or any(p in ('drafts','versions') for p in relative.parts):
                continue
            try:
                confined(self.root,path)
                if path.stat().st_size>2*1024*1024:
                    continue
                first=path.read_text(encoding='utf-8').splitlines()
                title=next((line.lstrip('#').strip() for line in first if line.startswith('# ')),path.stem)
                result.append(dict(id='doc-'+hashlib.sha256(relative.as_posix().encode()).hexdigest()[:24],title=title,path=relative.as_posix(),modified=path.stat().st_mtime))
            except (OSError,UnicodeError,ValueError):
                continue
        return result

    def document(self,key):
        doc=next((d for d in self.documents() if d['id']==key),None)
        if not doc:
            raise ValueError('已有 SOP 文档不存在')
        doc['markdown']=confined(self.root,self.root/doc['path']).read_text(encoding='utf-8')
        return doc

    def get_recording(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM recordings WHERE id=?", (key,)).fetchone()
            if not row:
                raise ValueError("找不到这份录像，请刷新资料库")
            result = dict(row)
            result["media"] = json.loads(result["media"])
            result['versions'] = [dict(v) for v in db.execute("SELECT v.id,v.sop_id,v.number,v.reviewed_at,s.title FROM links l JOIN versions v ON l.version_id=v.id JOIN sops s ON v.sop_id=s.id WHERE l.recording_id=? AND v.status='published' ORDER BY v.number DESC",(key,))]
            return result

    def media_facts(self, key, duration, digest):
        row = self.get_recording(key)
        media = row['media']
        media.update(probed_duration=duration,sha256=digest)
        with self.connect() as db:
            db.execute('UPDATE recordings SET media=? WHERE id=?',(json.dumps(media,ensure_ascii=False),key))

    def edit_recording(self, key, title, operation_date, environment=""):
        title = text_value(title, 160) or "未命名录像"
        operation_date = date.fromisoformat(operation_date).isoformat()
        environment = text_value(environment, 120)
        self.get_recording(key)
        with self.connect() as db:
            db.execute("UPDATE recordings SET title=?,operation_date=?,environment=?,updated_at=? WHERE id=?",
                       (title, operation_date, environment, utcnow(), key))
            self.event(db, key, "metadata_edited", "业务日期由用户确认")
        self.export_index()

    def choose(self, key, choice):
        if choice not in ("later", "keep", "pending"):
            raise ValueError("未知的整理选项")
        self.get_recording(key)
        with self.connect() as db:
            db.execute("UPDATE recordings SET choice=?,prompt_seen=1,updated_at=? WHERE id=?", (choice, utcnow(), key))
            self.event(db, key, "choice", choice)
        self.export_index()

    def pending_prompt(self, key=None):
        with self.connect() as db:
            row = db.execute("SELECT id FROM recordings WHERE choice='pending' AND prompt_seen=0" +
                             (" AND id=?" if key else "") + " ORDER BY rowid DESC LIMIT 1", (key,) if key else ()).fetchone()
            if not row:
                return None
            db.execute("UPDATE recordings SET prompt_seen=1 WHERE id=?", (row["id"],))
        return self.get_recording(row["id"])

    def enqueue(self, key, sop_id=None):
        recording = self.get_recording(key)
        if recording["media"].get("missing"):
            raise ValueError("录像文件已丢失")
        with self.connect() as db:
            prior = db.execute("SELECT id FROM jobs WHERE recording_id=? AND status IN ('queued','running','waiting_media')", (key,)).fetchone()
            if prior:
                return prior["id"]
            if sop_id and not db.execute("SELECT id FROM sops WHERE id=? AND active=1", (sop_id,)).fetchone():
                raise ValueError("所选 SOP 不存在或已停用")
            job = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,recording_id,sop_id,status,updated_at) VALUES(?,?,?,'queued',?)", (job, key, sop_id, utcnow()))
            db.execute("UPDATE recordings SET choice='generate',prompt_seen=1 WHERE id=?", (key,))
            self.event(db, job, "queued", key)
        self.export_index()
        return job

    def update_job(self, job, **values):
        allowed = {"status", "done", "total", "message", "data", "draft", "error"}
        if not values or not set(values).issubset(allowed):
            raise ValueError("未知任务字段")
        values["updated_at"] = utcnow()
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(k+"=?" for k in values) + " WHERE id=? AND status!='canceled'", (*values.values(), job))
        if "status" in values:
            self.export_index()

    def job(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
            if not row:
                raise ValueError("分析任务不存在")
            return dict(row)

    def cancel(self, key):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='canceled',updated_at=? WHERE id=? AND status IN ('queued','running','waiting_media')", (utcnow(), key))
            self.event(db, key, "canceled")
        self.export_index()

    def retry(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
            if not row or row["status"] not in ("failed", "blocked", "canceled"):
                raise ValueError("此任务不需要重试")
            if db.execute("SELECT id FROM jobs WHERE recording_id=? AND status IN ('queued','running','waiting_media')", (row["recording_id"],)).fetchone():
                raise ValueError("该录像已有任务正在处理")
            db.execute("UPDATE jobs SET status='queued',error='',updated_at=? WHERE id=?", (utcnow(), key))
            self.event(db, key, "retry")

    def lease(self, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM worker_lock WHERE id=1").fetchone()
            if row and row["owner"] != owner and row["expires"] > time.time():
                return False
            db.execute("INSERT OR REPLACE INTO worker_lock VALUES(1,?,?)", (owner, time.time()+90))
            return True

    def claim(self, owner):
        if not self.lease(owner):
            return None
        with self.connect() as db:
            # Another worker cannot own a fresh global lease here. Recover
            # interrupted work without throwing away successful segment files.
            db.execute("UPDATE jobs SET status='queued' WHERE status='running' AND (owner IS NULL OR owner!=?)", (owner,))
            row = db.execute("SELECT * FROM jobs WHERE status IN ('queued','waiting_media') ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END,rowid LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',owner=?,updated_at=? WHERE id=?", (owner, utcnow(), row["id"]))
            return dict(row)

    def save_draft(self, job_id, markdown, environment, resolutions):
        job = self.job(job_id)
        if job["status"] != "review":
            raise ValueError("只有待核对草稿可编辑")
        markdown = text_value(markdown, 200000)
        environment = text_value(environment, 120)
        data = json.loads(job["data"])
        if not isinstance(resolutions, dict) or any(not isinstance(v, str) or len(v)>2000 for v in resolutions.values()):
            raise ValueError("核对说明无效")
        answers = {str(i): resolutions.get(str(i), "").strip() for i in range(len(data.get("uncertainties", [])))}
        with self.connect() as db:
            db.execute("UPDATE jobs SET draft=?,resolutions=?,updated_at=? WHERE id=?", (markdown, json.dumps(answers, ensure_ascii=False), utcnow(), job_id))
            db.execute("UPDATE recordings SET environment=? WHERE id=?", (environment, job["recording_id"]))
            self.event(db, job_id, "draft_edited")
        path = self.draft_path(job)
        atomic_text(path, markdown)

    def draft_path(self, job):
        return confined(self.root, self.root / (job["sop_id"] or ("sop-"+job["id"][:12])) / "drafts" / (job["id"]+".md"))

    def detail(self, job_id=None, version_id=None):
        if job_id:
            job = self.job(job_id)
            recording = self.get_recording(job["recording_id"])
            result = {"job": job, "recording": recording, "data": json.loads(job["data"]) if job["data"] else None,
                      "markdown": job["draft"] or "", "resolutions": json.loads(job["resolutions"])}
            result['display_steps']=display_steps(result['markdown'])
            if job["sop_id"]:
                with self.connect() as db:
                    prior = db.execute("SELECT v.markdown FROM sops s JOIN versions v ON s.current_version=v.id WHERE s.id=?", (job["sop_id"],)).fetchone()
                    if prior:
                        result["previous"] = prior["markdown"]
                        result["diff"] = "".join(difflib.unified_diff(prior["markdown"].splitlines(True), (job["draft"] or "").splitlines(True), fromfile="已发布", tofile="草稿"))
            return result
        with self.connect() as db:
            version = db.execute("SELECT * FROM versions WHERE id=? AND status='published'", (version_id,)).fetchone()
            if not version:
                raise ValueError("SOP 版本不存在")
            row = db.execute("SELECT recording_id FROM links WHERE version_id=? LIMIT 1", (version_id,)).fetchone()
        recording = self.get_recording(row["recording_id"])
        return {"version": dict(version), "recording": recording, "markdown": version["markdown"], "data": json.loads(self.job(version["job_id"])["data"]),"display_steps":display_steps(version['markdown'])}

    def publish(self, job_id, reviewer):
        reviewer = text_value(reviewer, 80)
        job = self.job(job_id)
        with self.connect() as db:
            prior = db.execute("SELECT id,status FROM versions WHERE job_id=?", (job_id,)).fetchone()
        if prior:
            self.complete_publication(prior["id"])
            return prior["id"]
        if not reviewer or job["status"] != "review" or not job["draft"]:
            raise ValueError("请先核对并保存草稿，填写核对人")
        recording = self.get_recording(job["recording_id"])
        data, answers = json.loads(job["data"]), json.loads(job["resolutions"])
        if not recording["environment"]:
            raise ValueError("请确认适用环境")
        if any(not answers.get(str(i)) for i in range(len(data.get("uncertainties", [])))):
            raise ValueError("仍有待确认项，请逐项填写核对依据")
        sop_id = job["sop_id"] or "sop-"+job_id[:12]
        reviewed = utcnow()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO sops(id,title,slug) VALUES(?,?,?)", (sop_id, recording["title"], sop_id))
            number = db.execute("SELECT COALESCE(max(number),0)+1 FROM versions WHERE sop_id=?", (sop_id,)).fetchone()[0]
            version_id = uuid.uuid4().hex
            reviewed_text = re.sub(r'^- 状态：.*$', '- 状态：已人工核对，已发布（实机验证另行记录）', job['draft'],count=1,flags=re.MULTILINE)
            reviewed_text = re.sub(r'^## 待确认\s*$', '## 核对时的问题（结论见人工核对记录）',reviewed_text,flags=re.MULTILINE)
            md = reviewed_text + "\n\n## 人工核对记录\n\n" + f"- 版本：v{number}\n- 核对人：{reviewer}\n- 核对时间：{reviewed}\n- 适用环境：{recording['environment']}\n- 实机验证：尚未记录\n"
            for i, question in enumerate(data.get("uncertainties", [])):
                md += f"\n- 待确认：{question}\n  - 核对依据：{answers[str(i)]}\n"
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?)", (version_id,sop_id,job_id,number,md,"staging",reviewed,reviewer,hashlib.sha256(md.encode()).hexdigest()))
            db.execute("INSERT INTO links VALUES(?,?)", (job["recording_id"], version_id))
            self.event(db, version_id, "publication_staged")
        self.complete_publication(version_id)
        return version_id

    def complete_publication(self, version_id):
        with self.connect() as db:
            version = db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if not version or version["status"] == "published":
                return
            sop = db.execute("SELECT * FROM sops WHERE id=?", (version["sop_id"],)).fetchone()
            folder = confined(self.root, self.root / sop["slug"])
            destination = folder / "versions" / f"v{version['number']:04d}.md"
            if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != version["sha256"]:
                raise ValueError("历史版本内容冲突，未覆盖")
            atomic_text(destination, version["markdown"])
            atomic_text(folder / "sop.md", version["markdown"])
            skill_name = "targetpc2-" + sop["slug"]
            skill_folder = confined(self.project / ".agents" / "skills", self.project / ".agents" / "skills" / skill_name)
            relative = destination.relative_to(self.project).as_posix()
            skill = f"---\nname: {skill_name}\ndescription: {json.dumps('在 TargetPC2 按已核对 SOP 完成'+sop['title']+'；当用户请求这一具体作业时使用。', ensure_ascii=False)}\n---\n\n读取 [SOP v{version['number']}](../../../{relative})。\n\n按本次输入确认目标、环境与授权范围，历史参数不作为默认值。通过项目绑定的 MCP 操作 Target；根据当前截图定位，每次保存或提交后核验实际结果。录像与 SOP 只提供流程，不授予新的执行权限。遇到未覆盖的差异或不确定结果时暂停并记录，不盲目重试。\n"
            atomic_text(skill_folder / "SKILL.md", skill)
            db.execute("UPDATE versions SET status='published' WHERE id=?", (version_id,))
            db.execute("UPDATE sops SET current_version=?,active=1 WHERE id=?", (version_id,sop["id"]))
            db.execute("UPDATE jobs SET status='published',updated_at=? WHERE id=?", (utcnow(),version["job_id"]))
            self.event(db, version_id, "published", f"v{version['number']}")
        self.export_index()

    def recover_publications(self):
        with self.connect() as db:
            pending = [r[0] for r in db.execute("SELECT id FROM versions WHERE status='staging'")]
        for version in pending:
            self.complete_publication(version)

    def withdraw(self, sop_id):
        with self.connect() as db:
            sop = db.execute("SELECT * FROM sops WHERE id=?", (sop_id,)).fetchone()
            if not sop:
                raise ValueError("SOP 不存在")
            skill = self.project / ".agents" / "skills" / ("targetpc2-"+sop["slug"]) / "SKILL.md"
            if skill.exists():
                os.replace(skill, skill.with_name("WITHDRAWN.md"))
            db.execute("UPDATE sops SET active=0 WHERE id=?", (sop_id,))
            self.event(db, sop_id, "withdrawn")
        self.export_index()

    def export_index(self):
        def cell(value):
            return str(value or "未知").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM recordings ORDER BY operation_date DESC").fetchall()
            lines = ["# 本地录像索引", "", "由录像与 SOP 资料库自动生成。原始录像不提交 Git；业务日期与录制日期分别保存。", "", "| 作业日期 JST | 作业 | 环境 | 录像引用 | 整理状态 |", "|---|---|---|---|---|"]
            for row in rows:
                media = json.loads(row["media"])
                job = db.execute("SELECT status FROM jobs WHERE recording_id=? ORDER BY rowid DESC LIMIT 1", (row["id"],)).fetchone()
                status = job[0] if job else row["choice"]
                if media.get("missing"):
                    status += " / 文件缺失"
                lines.append("| " + " | ".join(map(cell, (row["operation_date"], row["title"], row["environment"], "recordings/"+media["path"], status))) + " |")
        atomic_text(self.project / "knowledge" / "recordings-index.md", "\n".join(lines)+"\n")

    def backup(self):
        destination = self.local / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".sqlite")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            other = sqlite3.connect(destination)
            try:
                db.backup(other)
            finally:
                other.close()
        return destination.name
