"""Managed GLB assets, fixed-scene Blender jobs, and explicit VideoForge shot binding.

No generated Python, model API or automatic camera/approval repair is involved.
All writable paths are server-owned descendants of the database's studio folder.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
import time
from typing import Any
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse


def _ok(data: Any = None) -> dict[str, Any]:
    return {"code":"000000","message":"success","data":data}


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status,detail={"code":"STUDIO_ERROR","message":message})


def _hash(path: Path) -> str:
    value=hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda:file.read(1024*1024),b""):
            value.update(chunk)
    return value.hexdigest()


def _json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    os.replace(temp,path)


def _identifier(value: Any) -> str:
    if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",value):
        raise _error(422,"ID只能包含字母、数字、下划线和短横线")
    return value


def _number(value: Any) -> float:
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or abs(value)>100000:
        raise _error(422,"场景数值必须是有限数字且绝对值不超过100000")
    return float(value)


def _vector(value: Any, size: int) -> list[float]:
    if not isinstance(value,list) or len(value)!=size:
        raise _error(422,f"向量必须包含{size}个数字")
    return [_number(item) for item in value]


def _quaternion(value: Any) -> list[float]:
    q=_vector(value,4)
    norm=math.sqrt(sum(x*x for x in q))
    if norm<1e-8:
        raise _error(422,"旋转四元数不能全为0")
    return [x/norm for x in q]


def _scale(value: Any) -> list[float]:
    scale=_vector(value,3)
    if any(x<=0 or x>10000 for x in scale):
        raise _error(422,"缩放必须大于0且不超过10000")
    return scale


def _object(value: Any, label: str) -> dict[str,Any]:
    if not isinstance(value,dict):
        raise _error(422,f"{label}必须是JSON对象")
    return value


def validate_scene(value: Any) -> dict[str,Any]:
    """Normalize untrusted wire JSON without deriving or changing camera placement."""
    if not isinstance(value,dict) or value.get("schema")!="videoforge.scene/1":
        raise _error(422,"不支持的场景格式，需要videoforge.scene/1")
    if value.get("coordinates")!={"up":"Z","handedness":"right","unit":"meter"}:
        raise _error(422,"场景坐标必须是米制、右手、Z-up")
    scene_id=_identifier(value.get("scene_id"))
    revision=value.get("revision")
    if type(revision) is not int or not 1<=revision<=1_000_000:
        raise _error(422,"revision必须是正整数")
    render=_object(value.get("render",{}),"render")
    defaults={"width":640,"height":360,"fps":24,"frame_count":72}
    limits={"width":(64,1920),"height":(64,1920),"fps":(1,60),"frame_count":(1,1800)}
    result_render={}
    for field,default in defaults.items():
        number=render.get(field,default)
        low,high=limits[field]
        if type(number) is not int or not low<=number<=high:
            raise _error(422,f"{field}超出支持范围{low}–{high}")
        result_render[field]=number
    if result_render["width"]%2 or result_render["height"]%2 or result_render["width"]*result_render["height"]>1920*1080:
        raise _error(422,"视频宽高须为偶数，像素总量不超过1920×1080")
    if result_render["frame_count"]/result_render["fps"]>60:
        raise _error(422,"单镜头最长60秒")
    assets=value.get("assets")
    instances=value.get("instances")
    if not isinstance(assets,list) or not 1<=len(assets)<=20:
        raise _error(422,"需要1–20个已登记GLB资产")
    if not isinstance(instances,list) or not 1<=len(instances)<=100:
        raise _error(422,"需要1–100个模型实例")
    normalized_assets=[{"id":_identifier(item.get("id"))} for item in assets if isinstance(item,dict)]
    asset_ids={item["id"] for item in normalized_assets}
    if len(normalized_assets)!=len(assets) or len(asset_ids)!=len(assets):
        raise _error(422,"资产ID重复或格式错误")
    normalized_instances=[]
    for item in instances:
        if not isinstance(item,dict):
            raise _error(422,"实例格式错误")
        asset_id=_identifier(item.get("asset_id"))
        if asset_id not in asset_ids:
            raise _error(422,"实例引用了未列入场景的资产")
        normalized_instances.append({"id":_identifier(item.get("id")),"asset_id":asset_id,
            "position":_vector(item.get("position"),3),"quaternion":_quaternion(item.get("quaternion")),
            "scale":_scale(item.get("scale"))})
    instance_ids={item["id"] for item in normalized_instances}
    if len(instance_ids)!=len(instances):
        raise _error(422,"实例ID必须唯一")

    def keys(raw: Any,camera: bool) -> list[dict[str,Any]]:
        if not isinstance(raw,list) or not 1<=len(raw)<=1800:
            raise _error(422,"轨迹需要1–1800个关键帧")
        normalized=[]
        previous=-1
        for item in raw:
            if not isinstance(item,dict):
                raise _error(422,"关键帧格式错误")
            frame=item.get("frame")
            if type(frame) is not int or not 0<=frame<result_render["frame_count"] or frame<=previous:
                raise _error(422,"关键帧号须严格递增且处于渲染帧范围内")
            previous=frame
            key={"frame":frame,"position":_vector(item.get("position"),3)}
            if camera:
                key["target"]=_vector(item.get("target"),3)
                key["roll"]=_number(item.get("roll",0))
                key["vertical_fov"]=_number(item.get("vertical_fov",fov))
                if not 1<=key["vertical_fov"]<=150:raise _error(422,"关键帧垂直FOV范围为1–150度")
                if sum((a-b)**2 for a,b in zip(key["position"],key["target"]))<1e-10:
                    raise _error(422,"相机位置与目标不能重合")
            else:
                key["quaternion"]=_quaternion(item.get("quaternion"))
                key["scale"]=_scale(item.get("scale"))
            normalized.append(key)
        if normalized[0]["frame"]!=0:
            raise _error(422,"轨迹必须从第0帧开始；最后关键帧之后保持末姿态")
        return normalized

    camera=_object(value.get("camera",{}),"camera")
    fov=_number(camera.get("vertical_fov",35))
    if not 1<=fov<=150:
        raise _error(422,"垂直FOV范围为1–150度")
    tracks=value.get("object_tracks",[])
    if not isinstance(tracks,list) or len(tracks)>100:
        raise _error(422,"对象轨道格式错误")
    normalized_tracks=[]
    seen=set()
    for track in tracks:
        if not isinstance(track,dict) or track.get("instance_id") not in instance_ids or track["instance_id"] in seen:
            raise _error(422,"对象轨道须引用唯一的已存在实例")
        seen.add(track["instance_id"])
        normalized_tracks.append({"instance_id":track["instance_id"],"keys":keys(track.get("keys"),False)})
    return {"schema":"videoforge.scene/1","scene_id":scene_id,"revision":revision,
        "coordinates":{"up":"Z","handedness":"right","unit":"meter"},"assets":normalized_assets,
        "instances":normalized_instances,"camera":{"vertical_fov":fov,"keys":keys(camera.get("keys"),True)},
        "object_tracks":normalized_tracks,"render":result_render}


def validate_glb(raw: bytes) -> None:
    """Accept self-contained GLB 2 only; reject network/file references in glTF JSON."""
    if len(raw)<20 or raw[:4]!=b"glTF":
        raise _error(422,"文件不是有效的GLB")
    _,version,total=struct.unpack("<4sII",raw[:12])
    if version!=2 or total!=len(raw):
        raise _error(422,"仅支持完整的GLB 2文件")
    offset=12
    document=None
    while offset+8<=len(raw):
        length,kind=struct.unpack("<II",raw[offset:offset+8]);offset+=8
        if length%4 or offset+length>len(raw):
            raise _error(422,"GLB数据块损坏")
        if kind==0x4E4F534A:
            try:
                document=json.loads(raw[offset:offset+length].rstrip(b"\x00 \t\r\n"))
            except Exception:
                raise _error(422,"GLB JSON损坏") from None
        offset+=length
    if offset!=len(raw) or not isinstance(document,dict) or not isinstance(document.get("asset"),dict) or document["asset"].get("version")!="2.0":
        raise _error(422,"GLB结构不完整")
    for collection in ("buffers","images"):
        entries=document.get(collection,[])
        if not isinstance(entries,list):raise _error(422,"GLB集合格式错误")
        for item in entries:
            if not isinstance(item,dict):raise _error(422,"GLB资源格式错误")
            uri=item.get("uri")
            if uri is not None and (not isinstance(uri,str) or not uri.startswith("data:")):
                raise _error(422,"GLB必须自包含，不能引用外部文件或URL")
    if not isinstance(document.get("nodes",[]),list):raise _error(422,"GLB节点格式错误")
    if len(document.get("nodes",[]))>10000:
        raise _error(422,"模型节点过多")


class Cancelled(Exception):
    pass


class StudioService:
    """One local render worker with durable receipts and cancelable child processes."""
    def __init__(self,db: Any,*,root: Path | None = None,blender: str | None = None,
                 ffmpeg: str | None = None,ffprobe: str | None = None):
        self.db=db
        self.root=(root or Path(db.db_path).resolve().parent/"studio_bridge").resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.blender=blender or os.environ.get("VIDEOFORGE_BLENDER","")
        if not self.blender:
            from core.previz_blender import blender_path
            self.blender=blender_path()
        self.ffmpeg=ffmpeg or os.environ.get("VIDEOFORGE_FFMPEG","") or shutil.which("ffmpeg") or ""
        self.ffprobe=ffprobe or os.environ.get("VIDEOFORGE_FFPROBE","") or shutil.which("ffprobe") or ""
        if not self.ffmpeg:
            from core.ffmpeg_manager import get_ffmpeg_for_postprocess
            self.ffmpeg=get_ffmpeg_for_postprocess()
        if not self.ffprobe and self.ffmpeg:
            sibling=Path(self.ffmpeg).with_name("ffprobe.exe" if os.name=="nt" else "ffprobe")
            if sibling.exists():self.ffprobe=str(sibling)
        self.renderer=Path(__file__).resolve().parents[1]/"data/studio_bridge/render_scene.py"
        self.lock=threading.RLock()
        self.jobs: dict[str,dict[str,Any]]={}
        self.events: dict[str,threading.Event]={}
        self.processes: dict[str,subprocess.Popen]={}
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix="vf-studio-render")
        for receipt in (self.root/"jobs").glob("*/job.json"):
            try:
                job=json.loads(receipt.read_text(encoding="utf-8"))
                if job.get("status") in ("queued","rendering","encoding","validating"):
                    job.update(status="interrupted",error="服务已重启，请重新提交渲染")
                    _json(receipt,job)
                self.jobs[job["job_id"]]=job
            except Exception:
                # A partial/corrupt receipt is never treated as a successful render.
                continue

    def _asset(self,asset_id: str) -> dict[str,Any]:
        asset_id=_identifier(asset_id)
        path=self.root/"assets"/asset_id/"asset.json"
        if not path.exists():raise _error(404,"资产不存在，请先上传GLB")
        return json.loads(path.read_text(encoding="utf-8"))

    def upload(self,name: str,raw: bytes) -> dict[str,Any]:
        if not raw or len(raw)>64*1024*1024:raise _error(413,"GLB文件上限64MiB")
        if not name.lower().endswith(".glb"):raise _error(422,"仅支持.glb文件")
        validate_glb(raw)
        sha=hashlib.sha256(raw).hexdigest()
        asset_id="asset_"+sha[:24]
        folder=self.root/"assets"/asset_id
        with self.lock:
            if not (folder/"asset.json").exists():
                folder.mkdir(parents=True,exist_ok=True)
                (folder/"model.glb").write_bytes(raw)
                clean=re.sub(r"[\x00-\x1f<>:\"/\\|?*]","_",Path(name).name)[:100]
                _json(folder/"asset.json",{"id":asset_id,"name":clean,"sha256":sha,"bytes":len(raw),
                    "file_url":f"/api/studio/assets/{asset_id}/file","created_at":time.time()})
        return self._asset(asset_id)

    def assets(self) -> list[dict[str,Any]]:
        return [self._asset(path.parent.name) for path in (self.root/"assets").glob("*/asset.json")]

    def save_scene(self,raw: Any) -> dict[str,Any]:
        scene=validate_scene(raw)
        for asset in scene["assets"]:self._asset(asset["id"])
        path=self.root/"scenes"/scene["scene_id"]/f"{scene['revision']:08d}.json"
        with self.lock:
            if path.exists() and json.loads(path.read_text(encoding="utf-8"))!=scene:
                raise _error(409,"该场景版本已存在；修改后请增加revision")
            if not path.exists():_json(path,scene)
        return scene

    def scene(self,scene_id: str,revision: int | None = None) -> dict[str,Any]:
        folder=self.root/"scenes"/_identifier(scene_id)
        files=sorted(folder.glob("*.json"))
        if revision is not None:
            if type(revision) is not int or not 1<=revision<=1_000_000:raise _error(422,"revision无效")
            path=folder/f"{revision:08d}.json"
        elif files:path=files[-1]
        else:raise _error(404,"场景不存在")
        if not path.exists():raise _error(404,"场景版本不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def scenes(self) -> list[dict[str,Any]]:
        result=[]
        for folder in (self.root/"scenes").glob("*"):
            if folder.is_dir():
                scene=self.scene(folder.name)
                result.append({"scene_id":scene["scene_id"],"revision":scene["revision"],"render":scene["render"]})
        return result

    def _target(self,project_id: str,shot_id: str) -> dict[str,Any]:
        project_id,shot_id=_identifier(project_id),_identifier(shot_id)
        shot=self.db.get_shot(shot_id)
        if not self.db.get_project(project_id) or not shot or shot.get("project_id")!=project_id:
            raise _error(404,"分镜不属于指定项目")
        return shot

    def submit(self,raw: Any,project_id: str | None = None,shot_id: str | None = None,
               idempotency_key: str | None = None) -> dict[str,Any]:
        if bool(project_id)!=bool(shot_id):raise _error(422,"绑定目标需同时提供project_id与shot_id")
        if project_id:self._target(project_id,shot_id)
        if not self.blender or not Path(self.blender).is_file():raise _error(503,"未找到Blender，请配置VIDEOFORGE_BLENDER")
        if not self.ffmpeg or not self.ffprobe:raise _error(503,"未找到ffmpeg/ffprobe")
        if not self.renderer.exists():raise _error(503,"缺少固定场景渲染器")
        scene=self.save_scene(raw)
        if idempotency_key is not None:idempotency_key=_identifier(idempotency_key)
        request_hash=hashlib.sha256(json.dumps({"scene":scene,"project_id":project_id,"shot_id":shot_id},
                                              sort_keys=True,separators=(",",":")).encode()).hexdigest()
        with self.lock:
            if idempotency_key is not None:
                previous=next((job for job in self.jobs.values() if job.get("idempotency_key")==idempotency_key),None)
                if previous:
                    if previous.get("request_sha256")!=request_hash:raise _error(409,"幂等键已用于另一份渲染请求")
                    return {"job_id":previous["job_id"],"status":previous["status"],"reused":True}
            if sum(job["status"] in ("queued","rendering","encoding","validating") for job in self.jobs.values())>=10:
                raise _error(429,"渲染队列已满")
            job_id="job_"+uuid.uuid4().hex
            folder=self.root/"jobs"/job_id
            resolved={}
            for asset in scene["assets"]:
                meta=self._asset(asset["id"])
                path=self.root/"assets"/asset["id"]/"model.glb"
                if _hash(path)!=meta["sha256"]:raise _error(409,"受管资产校验失败，请重新登记")
                resolved[asset["id"]]=str(path)
            _json(folder/"scene.json",scene)
            _json(folder/"input.json",{"scene":scene,"resolved_assets":resolved})
            job={"job_id":job_id,"status":"queued","progress":0,"created_at":time.time(),
                 "scene_id":scene["scene_id"],"scene_revision":scene["revision"],"project_id":project_id,"shot_id":shot_id,
                 "idempotency_key":idempotency_key,"request_sha256":request_hash}
            self.jobs[job_id]=job
            self.events[job_id]=threading.Event()
            self._persist(job_id)
            self.pool.submit(self._work,job_id)
        return {"job_id":job_id,"status":"queued"}

    def _persist(self,job_id: str) -> None:
        _json(self.root/"jobs"/job_id/"job.json",self.jobs[job_id])

    def _set(self,job_id: str,**values: Any) -> None:
        with self.lock:
            self.jobs[job_id].update(values)
            self._persist(job_id)

    def get(self,job_id: str) -> dict[str,Any]:
        job_id=_identifier(job_id)
        with self.lock:
            if job_id not in self.jobs:raise _error(404,"渲染任务不存在")
            job=copy.deepcopy(self.jobs[job_id])
        if job["status"]=="rendering":
            folder=self.root/"jobs"/job_id
            spec=json.loads((folder/"scene.json").read_text(encoding="utf-8"))
            count=len(list((folder/"frames").glob("frame_*.png")))
            job["progress"]=min(.8,.05+.75*count/spec["render"]["frame_count"])
        return job

    def cancel(self,job_id: str) -> dict[str,Any]:
        job=self.get(job_id)
        with self.lock:
            if job["status"] in ("queued","rendering","encoding","validating"):
                self.events[job_id].set()
                process=self.processes.get(job_id)
                if process and process.poll() is None:
                    try:process.terminate()
                    except OSError:pass  # Worker owns the wait/kill and final cancelled receipt.
                self._set(job_id,cancel_requested=True)
        return self.get(job_id)

    def _run(self,job_id: str,args: list[str],log_name: str,timeout: int) -> None:
        if self.events[job_id].is_set():raise Cancelled()
        folder=self.root/"jobs"/job_id
        with (folder/log_name).open("wb") as output:
            process=subprocess.Popen(args,stdout=output,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            with self.lock:self.processes[job_id]=process
            started=time.monotonic()
            try:
                while process.poll() is None:
                    if self.events[job_id].is_set() or time.monotonic()-started>timeout:
                        process.terminate()
                        try:process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill();process.wait(timeout=3)
                        if self.events[job_id].is_set():raise Cancelled()
                        raise RuntimeError("渲染子进程超时")
                    try:process.wait(timeout=.2)
                    except subprocess.TimeoutExpired:pass
                if self.events[job_id].is_set():raise Cancelled()
                if process.returncode:raise RuntimeError(f"子进程失败，退出码{process.returncode}，详见{log_name}")
            finally:
                with self.lock:self.processes.pop(job_id,None)

    def _work(self,job_id: str) -> None:
        folder=self.root/"jobs"/job_id
        try:
            if self.events[job_id].is_set():raise Cancelled()
            self._set(job_id,status="rendering",progress=.05)
            self._run(job_id,[self.blender,"--background","--factory-startup","--disable-autoexec",
                "--python-exit-code","3","--python",str(self.renderer),"--",str(folder/"input.json"),str(folder)],"blender.log",1800)
            scene=json.loads((folder/"scene.json").read_text(encoding="utf-8"))
            render=scene["render"]
            self._set(job_id,status="encoding",progress=.83)
            self._run(job_id,[self.ffmpeg,"-hide_banner","-loglevel","error","-y","-framerate",str(render["fps"]),
                "-start_number","0","-i",str(folder/"frames/frame_%06d.png"),"-frames:v",str(render["frame_count"]),
                "-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p","-movflags","+faststart",str(folder/"frames.mp4")],"ffmpeg.log",600)
            self._set(job_id,status="validating",progress=.93)
            probe_file=folder/"probe.json"
            with probe_file.open("wb") as output:
                result=subprocess.run([self.ffprobe,"-v","error","-count_frames","-select_streams","v:0",
                    "-show_entries","stream=width,height,r_frame_rate,nb_read_frames:format=duration","-of","json",str(folder/"frames.mp4")],
                    stdout=output,stderr=subprocess.DEVNULL,timeout=60,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            if result.returncode:raise RuntimeError("编码视频无法解码校验")
            probe=json.loads(probe_file.read_text(encoding="utf-8"));stream=probe["streams"][0]
            if (stream["width"],stream["height"],int(stream["nb_read_frames"]))!=(render["width"],render["height"],render["frame_count"]):
                raise RuntimeError("视频分辨率或帧数与请求不符")
            numerator,denominator=map(int,stream["r_frame_rate"].split("/"))
            if abs(numerator/denominator-render["fps"])>.001:raise RuntimeError("视频帧率与请求不符")
            duration=float(probe["format"]["duration"])
            if abs(duration-render["frame_count"]/render["fps"])>.05:raise RuntimeError("视频时长与请求不符")
            samples=json.loads((folder/"samples.json").read_text(encoding="utf-8"))
            if len(samples["frames"])!=render["frame_count"]:raise RuntimeError("相机采样记录不完整")
            outputs=[]
            for kind,path in [("first_frame",folder/"frames/frame_000000.png"),("video",folder/"frames.mp4")]:
                outputs.append({"kind":kind,"path":str(path),"sha256":_hash(path),"bytes":path.stat().st_size,
                    "url":f"/api/studio/renders/{job_id}/files/{path.name}"})
            manifest={"schema":"videoforge.render/1","job_id":job_id,"scene_id":scene["scene_id"],
                "scene_revision":scene["revision"],"scene_sha256":_hash(folder/"scene.json"),
                "camera_samples_sha256":samples["camera_samples_sha256"],"renderer_sha256":_hash(self.renderer),
                "assets":[{"id":asset["id"],"sha256":self._asset(asset["id"])["sha256"]} for asset in scene["assets"]],
                "blender_version":samples["blender_version"],"vertical_fov_actual":samples["vertical_fov_actual"],
                "coordinates":scene["coordinates"],"render":render,"duration":duration,"outputs":outputs,
                "validation":{"passed":True,"decoded_frames":int(stream["nb_read_frames"]),"network_calls":0,"paid_api_calls":0},
                "approval":"rendered_unapproved","samples_url":f"/api/studio/renders/{job_id}/files/samples.json"}
            if self.events[job_id].is_set():raise Cancelled()
            _json(folder/"manifest.json",manifest)
            self._set(job_id,status="succeeded",progress=1,manifest=manifest,finished_at=time.time())
            job=self.get(job_id)
            if job.get("project_id"):
                self.bind(job_id,job["project_id"],job["shot_id"])
        except Cancelled:
            self._set(job_id,status="cancelled",progress=0,error="渲染已取消",finished_at=time.time())
        except Exception as error:
            self._set(job_id,status="failed",error=str(error)[:500],finished_at=time.time())

    def bind(self,job_id: str,project_id: str,shot_id: str) -> dict[str,Any]:
        job=self.get(job_id)
        if job["status"]!="succeeded":raise _error(409,"只有渲染并校验成功的任务才能绑定")
        shot=self._target(project_id,shot_id)
        for item in job["manifest"]["outputs"]:
            if _hash(Path(item["path"]))!=item["sha256"]:raise _error(409,"渲染文件hash变化，拒绝绑定")
        first=next(item["path"] for item in job["manifest"]["outputs"] if item["kind"]=="first_frame")
        old={key:shot.get(key) for key in ("previz_status","previz_path","previz_notes","previz_camera")}
        _json(self.root/"jobs"/job_id/("binding-before-"+_identifier(shot_id)+"-"+uuid.uuid4().hex+".json"),old)
        note=json.dumps({"source":"external_studio","job_id":job_id,"scene_id":job["scene_id"],
                         "scene_revision":job["scene_revision"],"manifest_path":str(self.root/"jobs"/job_id/"manifest.json"),
                         "previous_notes":shot.get("previz_notes") or ""},ensure_ascii=False)
        # Preserve the user's camera field. A rendered preview still requires explicit approval.
        self.db.update_shot(shot_id,previz_path=first,previz_status="rendered",previz_notes=note)
        binding={"project_id":project_id,"shot_id":shot_id,"previz_path":first,"approval":"rendered_unapproved"}
        self._set(job_id,binding=binding)
        return binding

    def file(self,job_id: str,name: str) -> Path:
        self.get(job_id)
        folder=self.root/"jobs"/_identifier(job_id)
        if name in ("frames.mp4","manifest.json","samples.json"):
            path=folder/name
        elif re.fullmatch(r"frame_[0-9]{6}\.png",name):path=folder/"frames"/name
        else:raise _error(404,"输出文件不存在")
        if not path.is_file():raise _error(404,"输出文件尚未生成")
        return path

    def health(self) -> dict[str,Any]:
        with self.lock:
            queued=sum(job["status"] in ("queued","rendering","encoding","validating") for job in self.jobs.values())
        return {"ready":bool(self.blender and Path(self.blender).is_file() and self.ffmpeg and self.ffprobe and self.renderer.is_file()),
                "blender_available":bool(self.blender and Path(self.blender).is_file()),
                "ffmpeg_available":bool(self.ffmpeg),"ffprobe_available":bool(self.ffprobe),
                "schema":"videoforge.scene/1","max_asset_bytes":64*1024*1024,
                "queued":queued}

    def close(self) -> None:
        with self.lock:
            for job_id,event in self.events.items():
                if self.jobs[job_id]["status"] in ("queued","rendering","encoding","validating"):event.set()
        self.pool.shutdown(wait=True,cancel_futures=False)


def create_studio_router(db: Any) -> APIRouter:
    """Mount the managed local studio API on an existing VideoForge FastAPI app."""
    service=StudioService(db)
    router=APIRouter(prefix="/api/studio",tags=["VideoForge studio"])
    router.add_event_handler("shutdown",service.close)

    @router.get("/health")
    def health():return _ok(service.health())

    @router.get("/assets")
    def assets():return _ok({"assets":service.assets()})

    @router.post("/assets")
    async def upload(file: UploadFile = File(...)):
        chunks=[];size=0
        while chunk:=await file.read(1024*1024):
            size+=len(chunk)
            if size>64*1024*1024:raise _error(413,"GLB文件上限64MiB")
            chunks.append(chunk)
        return _ok(service.upload(file.filename or "model.glb",b"".join(chunks)))

    @router.get("/assets/{asset_id}/file")
    def asset_file(asset_id: str):
        service._asset(asset_id)
        return FileResponse(service.root/"assets"/_identifier(asset_id)/"model.glb",media_type="model/gltf-binary")

    @router.get("/scenes")
    def scenes():return _ok({"scenes":service.scenes()})

    @router.post("/scenes")
    def save_scene(body: dict[str,Any]):return _ok({"scene":service.save_scene(body)})

    @router.get("/scenes/{scene_id}")
    def scene(scene_id: str,revision: int | None = None):return _ok({"scene":service.scene(scene_id,revision)})

    @router.post("/renders")
    def render(body: dict[str,Any]):return _ok(service.submit(body.get("scene"),body.get("project_id"),body.get("shot_id"),body.get("idempotency_key")))

    @router.get("/renders/{job_id}")
    def job(job_id: str):return _ok(service.get(job_id))

    @router.post("/renders/{job_id}/cancel")
    def cancel(job_id: str):return _ok(service.cancel(job_id))

    @router.post("/renders/{job_id}/bind")
    def bind(job_id: str,body: dict[str,Any]):return _ok(service.bind(job_id,body.get("project_id"),body.get("shot_id")))

    @router.get("/renders/{job_id}/files/{name}")
    def file(job_id: str,name: str):return FileResponse(service.file(job_id,name))

    return router
