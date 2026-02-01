import sys
import asyncio

# Fix for asyncio subprocess support on Windows
# MUST BE SET BEFORE OTHER IMPORTS
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import socketio
import uvicorn
from fastapi import FastAPI
import asyncio
import threading
import sys
import os
import json
import base64
import time
import re
from datetime import datetime
from pathlib import Path
import httpx

from google import genai
from google.genai import types



# Ensure we can import ada
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import ada
from authenticator import FaceAuthenticator
from kasa_agent import KasaAgent

from project_manager import ProjectManager

# Create a Socket.IO server
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    max_http_buffer_size=50_000_000,
)
app = FastAPI()
app_socketio = socketio.ASGIApp(sio, app)

import signal

# --- SHUTDOWN HANDLER ---
def signal_handler(sig, frame):
    print(f"\n[SERVER] Caught signal {sig}. Exiting gracefully...")
    # Clean up audio loop
    if audio_loop:
        try:
            print("[SERVER] Stopping Audio Loop...")
            audio_loop.stop() 
        except:
            pass
    # Force kill
    print("[SERVER] Force exiting...")
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Global state
audio_loop = None
loop_task = None
authenticator = None
kasa_agent = KasaAgent()
SETTINGS_FILE = "settings.json"

_project_manager = None

_image_rate_limit_until = 0.0


def _get_project_manager():
    global _project_manager
    if _project_manager is not None:
        return _project_manager

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    _project_manager = ProjectManager(project_root)
    return _project_manager

DEFAULT_SETTINGS = {
    "face_auth_enabled": False, # Default OFF as requested
    "tool_permissions": {
        "generate_cad": True,
        "run_web_agent": True,
        "write_file": True,
        "read_directory": True,
        "read_file": True,
        "create_project": True,
        "switch_project": True,
        "list_projects": True,
        "portainer_call": False,
        "list_mcp_tools": False,
    },
    "printers": [], # List of {host, port, name, type}
    "kasa_devices": [], # List of {ip, alias, model}
    "camera_flipped": False # Invert cursor horizontal direction
}

SETTINGS = DEFAULT_SETTINGS.copy()


def _env_true(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes")


def _enforce_tool_permission_defaults():
    try:
        perms = SETTINGS.get("tool_permissions")
        if not isinstance(perms, dict):
            return

        if not _env_true("ADA_CONFIRM_LIST_MCP_TOOLS", False):
            perms["list_mcp_tools"] = False

        if not _env_true("ADA_CONFIRM_PORTAINER_CALL", False):
            perms["portainer_call"] = False
    except Exception:
        return

def load_settings():
    global SETTINGS
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                loaded = json.load(f)
                # Merge with defaults to ensure new keys exist
                # Deep merge for tool_permissions would be better but shallow merge of top keys + tool_permissions check is okay for now
                for k, v in loaded.items():
                    if k == "tool_permissions" and isinstance(v, dict):
                         SETTINGS["tool_permissions"].update(v)
                    else:
                        SETTINGS[k] = v
            _enforce_tool_permission_defaults()
            print(f"Loaded settings: {SETTINGS}")
        except Exception as e:
            print(f"Error loading settings: {e}")

def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(SETTINGS, f, indent=4)
        print("Settings saved.")
    except Exception as e:
        print(f"Error saving settings: {e}")

# Load on startup
load_settings()
_enforce_tool_permission_defaults()

authenticator = None
kasa_agent = KasaAgent(known_devices=SETTINGS.get("kasa_devices"))
# tool_permissions is now SETTINGS["tool_permissions"]

@app.on_event("startup")
async def startup_event():
    import sys
    print(f"[SERVER DEBUG] Startup Event Triggered")
    print(f"[SERVER DEBUG] Python Version: {sys.version}")
    try:
        loop = asyncio.get_running_loop()
        print(f"[SERVER DEBUG] Running Loop: {type(loop)}")
        policy = asyncio.get_event_loop_policy()
        print(f"[SERVER DEBUG] Current Policy: {type(policy)}")
    except Exception as e:
        print(f"[SERVER DEBUG] Error checking loop: {e}")

    print("[SERVER] Startup: Initializing Kasa Agent...")
    await kasa_agent.initialize()

@app.get("/status")
async def status():
    return {"status": "running", "service": "A.D.A Backend"}


def _serialize_gemini_model(m):
    return {
        "name": getattr(m, "name", None),
        "display_name": getattr(m, "display_name", None) or getattr(m, "displayName", None),
        "description": getattr(m, "description", None),
        "supported_generation_methods": getattr(m, "supported_generation_methods", None)
        or getattr(m, "supportedGenerationMethods", None)
        or [],
        "input_token_limit": getattr(m, "input_token_limit", None) or getattr(m, "inputTokenLimit", None),
        "output_token_limit": getattr(m, "output_token_limit", None) or getattr(m, "outputTokenLimit", None),
    }


def _list_gemini_models_sync():
    client = _get_gemini_client()

    lister = getattr(getattr(client, "models", None), "list", None)
    if not callable(lister):
        raise RuntimeError("Gemini SDK does not support models.list() in this version")

    resp = lister()

    # google-genai may return an iterator/pager or an object containing a list.
    models = []
    try:
        for m in resp:
            models.append(_serialize_gemini_model(m))
    except TypeError:
        raw = getattr(resp, "models", None) or getattr(resp, "data", None) or []
        for m in raw:
            models.append(_serialize_gemini_model(m))

    models = [m for m in models if m.get("name")]
    models.sort(key=lambda x: x.get("name") or "")
    return models


@app.get("/gemini/models")
async def gemini_models():
    models = await asyncio.to_thread(_list_gemini_models_sync)
    return {"models": models}


async def _call_glama_chat(prompt: str) -> str:
    base = (os.getenv("MCP_GLAMA_BASE_URL") or "http://127.0.0.1:7441").rstrip("/")
    url = f"{base}/invoke"

    payload = {
        "tool": "chat_completion",
        "arguments": {
            "prompt": prompt,
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code >= 400:
        raise RuntimeError(f"glama_http_{resp.status_code}: {resp.text}")

    data = resp.json()
    result = (data or {}).get("result") or {}
    text = (result.get("response") or "").strip()
    return text


_gemini_client = None


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    _gemini_client = genai.Client(http_options={"api_version": "v1beta"}, api_key=api_key)
    return _gemini_client


async def _call_gemini_chat(prompt: str) -> str:
    def _run() -> str:
        client = _get_gemini_client()
        resp = client.models.generate_content(
            model=os.getenv("GEMINI_TEXT_MODEL") or "gemini-2.5-flash",
            contents=prompt,
        )
        text = getattr(resp, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        try:
            candidates = getattr(resp, "candidates", None) or []
            if candidates and getattr(candidates[0], "content", None):
                parts = getattr(candidates[0].content, "parts", None) or []
                joined = "".join([getattr(p, "text", "") for p in parts])
                if joined.strip():
                    return joined.strip()
        except Exception:
            pass
        return ""

    return await asyncio.to_thread(_run)


async def _call_mcp_imagen(prompt: str, reference_images=None):
    base = (os.getenv("MCP_IMAGEN_BASE_URL") or "http://mcp-imagen-light:8020").rstrip("/")
    timeout_s = float(os.getenv("MCP_IMAGEN_TIMEOUT_S") or 180)

    args = {
        "prompt": prompt,
        "approved": True,
    }

    # Optional: pass a single reference image if provided
    # mcp-imagen-light supports referenceImageBase64 and referenceImageDimensions.
    try:
        imgs = reference_images or []
        if imgs and isinstance(imgs[0], dict) and imgs[0].get("data"):
            args["referenceImageBase64"] = imgs[0].get("data")
    except Exception:
        pass

    invoke_payload = {
        "tool": "imagen_generate",
        "arguments": args,
    }

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(f"{base}/invoke", json=invoke_payload)
        resp.raise_for_status()
        data = resp.json() or {}
        result = data.get("result") or {}
        job_id = result.get("job_id") or result.get("jobId")
        if not job_id:
            raise RuntimeError(f"mcp-imagen: missing job_id in response: {data}")

        # Poll status until succeeded/failed
        start = time.time()
        status = None
        last_status_payload = None
        while True:
            if time.time() - start > timeout_s:
                raise RuntimeError(f"mcp-imagen: timeout waiting for job {job_id}")

            st = await client.get(f"{base}/imagen/jobs/{job_id}")
            st.raise_for_status()
            last_status_payload = st.json() or {}
            status = (last_status_payload.get("status") or "").lower()
            if status in ("succeeded", "success", "completed", "done"):
                break
            if status in ("failed", "error", "cancelled", "canceled"):
                raise RuntimeError(f"mcp-imagen: job failed: {last_status_payload}")
            await asyncio.sleep(1)

        # Fetch result payload
        rr = await client.get(f"{base}/imagen/jobs/{job_id}/result")
        rr.raise_for_status()
        rj = rr.json() or {}

        # Try multiple common shapes
        b64 = (
            rj.get("image_base64")
            or rj.get("imageBase64")
            or (rj.get("result") or {}).get("image_base64")
            or (rj.get("result") or {}).get("imageBase64")
        )
        mime = rj.get("mime") or rj.get("mime_type") or rj.get("mimeType") or "image/png"
        if b64:
            try:
                img_bytes = base64.b64decode(b64)
                return {"mime": mime, "bytes": img_bytes, "meta": {"job_id": job_id}}
            except Exception as e:
                raise RuntimeError(f"mcp-imagen: failed to decode base64 result: {e}") from e

        url = (
            rj.get("url")
            or rj.get("image_url")
            or rj.get("imageUrl")
            or (rj.get("result") or {}).get("url")
            or (rj.get("result") or {}).get("image_url")
            or (rj.get("result") or {}).get("imageUrl")
        )
        if not url:
            raise RuntimeError(f"mcp-imagen: unknown result shape: {rj}")

        # Download image bytes from the returned URL
        img_resp = await client.get(url)
        img_resp.raise_for_status()
        ct = img_resp.headers.get("content-type")
        if ct:
            mime = ct.split(";")[0].strip()
        return {"mime": mime, "bytes": img_resp.content, "meta": {"job_id": job_id, "url": url}}


def _get_active_project_manager():
    if audio_loop is not None and getattr(audio_loop, "project_manager", None) is not None:
        return audio_loop.project_manager
    return _get_project_manager()


async def _call_gemini_image(prompt: str, reference_images=None):
    def _run():
        client = _get_gemini_client()

        parts = [types.Part(text=prompt)]
        for img in (reference_images or []):
            if not isinstance(img, dict):
                continue
            b64 = img.get("data")
            mime = img.get("mime") or "image/png"
            if not b64:
                continue
            try:
                img_bytes = base64.b64decode(b64)
            except Exception:
                continue
            parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

        model = os.getenv("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"
        try:
            resp = client.models.generate_content(
                model=model,
                contents=types.Content(role="user", parts=parts),
            )
        except Exception as e:
            msg = str(e)
            if "404" in msg or "NOT_FOUND" in msg:
                raise RuntimeError(
                    f"{msg} | Set GEMINI_IMAGE_MODEL to an available image model (per docs, try 'gemini-2.5-flash-image')."
                ) from e
            raise

        candidates = getattr(resp, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in (getattr(content, "parts", None) or []):
                inline = getattr(part, "inline_data", None)
                if inline is not None:
                    data = getattr(inline, "data", None)
                    mime = getattr(inline, "mime_type", None) or "image/png"
                    if data:
                        return {"mime": mime, "bytes": data}
                blob = getattr(part, "blob", None)
                if blob is not None:
                    data = getattr(blob, "data", None)
                    mime = getattr(blob, "mime_type", None) or "image/png"
                    if data:
                        return {"mime": mime, "bytes": data}

        raise RuntimeError("No image returned by model")

    return await asyncio.to_thread(_run)

@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")
    await sio.emit('status', {'msg': 'Connected to A.D.A Backend'}, room=sid)

    async def _prewarm_tools():
        timeout_s = float(os.getenv("ADA_TOOL_INIT_TIMEOUT_S") or 2.0)
        try:
            one_mcp_url = (os.getenv("ONE_MCP_URL") or "").strip()
            if not one_mcp_url:
                await sio.emit('status', {'msg': 'Tools: unavailable (ONE_MCP_URL not set)'}, room=sid)
                return

            mcp = getattr(ada, "_get_one_mcp_client", None)
            if not callable(mcp):
                await sio.emit('status', {'msg': 'Tools: unavailable (missing _get_one_mcp_client)'}, room=sid)
                return

            client = mcp()
            if client is None:
                await sio.emit('status', {'msg': 'Tools: unavailable (failed to create MCP client)'}, room=sid)
                return

            data = await asyncio.wait_for(client.list_tools(), timeout=timeout_s)
            tools = (data or {}).get("tools") if isinstance(data, dict) else None
            if tools is None:
                tools = data
            count = len(tools) if isinstance(tools, list) else 0
            await sio.emit('status', {'msg': f'Tools: ready ({count} tools)'}, room=sid)
        except asyncio.TimeoutError:
            await sio.emit('status', {'msg': f'Tools: unavailable (init timeout after {timeout_s:.1f}s)'}, room=sid)
        except Exception as e:
            await sio.emit('status', {'msg': f'Tools: unavailable ({e})'}, room=sid)

    asyncio.create_task(_prewarm_tools())

    global authenticator

    # Callback for Auth Status
    async def on_auth_status(is_auth):
        print(f"[SERVER] Auth status change: {is_auth}")
        await sio.emit('auth_status', {'authenticated': is_auth})

    # Callback for Auth Camera Frames
    async def on_auth_frame(frame_b64):
        await sio.emit('auth_frame', {'image': frame_b64})

    # Initialize Authenticator if not already done
    if authenticator is None:
        authenticator = FaceAuthenticator(
            reference_image_path="reference.jpg",
            on_status_change=on_auth_status,
            on_frame=on_auth_frame
        )

    # Check if already authenticated or needs to start
    if authenticator.authenticated:
        await sio.emit('auth_status', {'authenticated': True})
    else:
        # Check Settings for Auth
        if SETTINGS.get("face_auth_enabled", False):
            await sio.emit('auth_status', {'authenticated': False})
            # Start the auth loop in background
            asyncio.create_task(authenticator.start_authentication_loop())
        else:
            # Bypass Auth
            print("Face Auth Disabled. Auto-authenticating.")
            # We don't change authenticator state to true to avoid confusion if re-enabled?
            # Or we should just tell client it's auth'd.
            await sio.emit('auth_status', {'authenticated': True})


@sio.event
async def list_gemini_models(sid, data=None):
    try:
        models = await asyncio.to_thread(_list_gemini_models_sync)
        return {"ok": True, "models": models}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")

@sio.event
async def start_audio(sid, data=None):
    global audio_loop, loop_task
    print(f"[SERVER] start_audio sid={sid} data_keys={list((data or {}).keys())}")
    try:
        await sio.emit('status', {'msg': 'start_audio received'}, room=sid)
    except Exception:
        pass
    
    # Optional: Block if not authenticated
    # Only block if auth is ENABLED and not authenticated
    if SETTINGS.get("face_auth_enabled", False):
        if authenticator and not authenticator.authenticated:
            print("Blocked start_audio: Not authenticated.")
            await sio.emit('error', {'msg': 'Authentication Required'})
            return {"ok": False, "error": "Authentication Required"}

    # ACK immediately to avoid client timeouts; do heavy init in background.
    async def _start_in_background():
        global audio_loop, loop_task
        print("Starting Audio Loop...")

        device_index = None
        device_name = None
        use_browser_audio = False
        if data:
            if 'device_index' in data:
                device_index = data['device_index']
            if 'device_name' in data:
                device_name = data['device_name']
            if 'use_browser_audio' in data:
                use_browser_audio = bool(data['use_browser_audio'])

        print(f"Using input device: Name='{device_name}', Index={device_index}, BrowserAudio={use_browser_audio}")

        try:
            if use_browser_audio:
                await sio.emit('status', {'msg': 'Browser audio: enabled (awaiting mic chunks...)'}, room=sid)
            else:
                await sio.emit('status', {'msg': 'Browser audio: disabled (using PyAudio mic)'}, room=sid)
        except Exception:
            pass

        if audio_loop:
            if loop_task and (loop_task.done() or loop_task.cancelled()):
                print("Audio loop task appeared finished/cancelled. Clearing and restarting...")
                audio_loop = None
                loop_task = None
            else:
                print("Audio loop already running. Re-connecting client to session.")
                await sio.emit('status', {'msg': 'A.D.A Already Running'}, room=sid)
                return

        def on_audio_data(data_bytes):
            if not data_bytes:
                return
            asyncio.create_task(sio.emit('assistant_audio_chunk', data_bytes, room=sid))
            step = max(1, len(data_bytes) // 64)
            viz = [b for i, b in enumerate(data_bytes[::step][:64])]
            asyncio.create_task(sio.emit('audio_data', {'data': viz}, room=sid))

        def on_audio_format(fmt):
            try:
                asyncio.create_task(sio.emit('assistant_audio_format', fmt, room=sid))
            except Exception:
                pass

        def on_cad_data(payload):
            asyncio.create_task(sio.emit('cad_data', payload, room=sid))

        def on_web_data(payload):
            asyncio.create_task(sio.emit('browser_frame', payload, room=sid))

        def on_transcription(payload):
            asyncio.create_task(sio.emit('transcription', payload, room=sid))

        def on_status(msg):
            asyncio.create_task(sio.emit('status', {'msg': msg}, room=sid))

        def on_tool_confirmation(payload):
            asyncio.create_task(sio.emit('tool_confirmation_request', payload, room=sid))

        def on_cad_status(status):
            if isinstance(status, dict):
                asyncio.create_task(sio.emit('cad_status', status, room=sid))
            else:
                asyncio.create_task(sio.emit('cad_status', {'status': status}, room=sid))

        def on_cad_thought(thought_text):
            asyncio.create_task(sio.emit('cad_thought', {'text': thought_text}, room=sid))

        def on_project_update(project_name):
            asyncio.create_task(sio.emit('project_update', {'project': project_name}, room=sid))

        def on_device_update(devices):
            asyncio.create_task(sio.emit('kasa_devices', devices, room=sid))

        def on_error(msg):
            asyncio.create_task(sio.emit('error', {'msg': msg}, room=sid))

        try:
            print(f"Initializing AudioLoop with device_index={device_index}")
            audio_loop = ada.AudioLoop(
                video_mode="none",
                on_audio_data=on_audio_data,
                on_audio_format=on_audio_format,
                on_cad_data=on_cad_data,
                on_web_data=on_web_data,
                on_transcription=on_transcription,
                on_tool_confirmation=on_tool_confirmation,
                on_status=on_status,
                on_cad_status=on_cad_status,
                on_cad_thought=on_cad_thought,
                on_project_update=on_project_update,
                on_device_update=on_device_update,
                on_error=on_error,
                input_device_index=device_index,
                input_device_name=device_name,
                kasa_agent=kasa_agent,
                use_browser_audio=use_browser_audio,
            )
            audio_loop.update_permissions(SETTINGS["tool_permissions"])

            try:
                await sio.emit(
                    'assistant_audio_format',
                    {
                        'sampleRate': getattr(ada, 'RECEIVE_SAMPLE_RATE', 24000),
                        'channels': 1,
                        'encoding': 'pcm_s16le',
                    },
                    room=sid,
                )
            except Exception:
                pass

            if data and data.get('muted', False):
                audio_loop.set_paused(True)

            loop_task = asyncio.create_task(audio_loop.run())

            def handle_loop_exit(task):
                try:
                    task.result()
                except asyncio.CancelledError:
                    print("Audio Loop Cancelled")
                    try:
                        asyncio.create_task(sio.emit('status', {'msg': 'Audio loop stopped'}, room=sid))
                    except Exception:
                        pass
                except Exception as e:
                    print(f"Audio Loop Crashed: {e}")
                    try:
                        asyncio.create_task(sio.emit('error', {'msg': f'Audio loop crashed: {str(e)}'}, room=sid))
                        asyncio.create_task(sio.emit('status', {'msg': f'Audio loop crashed: {str(e)}'}, room=sid))
                    except Exception:
                        pass

            loop_task.add_done_callback(handle_loop_exit)
            await sio.emit('status', {'msg': 'A.D.A Started'}, room=sid)
            asyncio.create_task(monitor_printers_loop())
        except Exception as e:
            print(f"CRITICAL ERROR STARTING ADA: {e}")
            traceback.print_exc()
            await sio.emit('error', {'msg': f"Failed to start: {str(e)}"}, room=sid)
            audio_loop = None

    bg_task = asyncio.create_task(_start_in_background())

    def _bg_done(task: asyncio.Task):
        try:
            task.result()
        except asyncio.CancelledError:
            print("[SERVER] start_audio background task cancelled")
        except Exception as e:
            print(f"[SERVER] start_audio background task crashed: {e}")
            import traceback
            traceback.print_exc()
            try:
                asyncio.create_task(sio.emit('error', {'msg': f"start_audio failed: {str(e)}"}, room=sid))
            except Exception:
                pass

    bg_task.add_done_callback(_bg_done)
    return {"ok": True, "status": "starting"}


@sio.event
async def mic_audio_chunk(sid, data):
    global audio_loop
    if not audio_loop:
        await sio.emit('error', {'msg': 'Audio loop is not started. Click power/connect first.'}, room=sid)
        return

    if not getattr(audio_loop, 'use_browser_audio', False):
        await sio.emit('error', {'msg': 'Audio loop is not in browser-audio mode.'}, room=sid)
        return

    if not audio_loop.out_queue:
        return

    pcm_bytes = None
    if isinstance(data, (bytes, bytearray, memoryview)):
        pcm_bytes = bytes(data)
    elif isinstance(data, dict):
        if isinstance(data.get('pcm16'), (bytes, bytearray, memoryview)):
            pcm_bytes = bytes(data.get('pcm16'))
        # Node-style Buffer JSON: { type: 'Buffer', data: [..] }
        elif data.get('type') == 'Buffer' and isinstance(data.get('data'), list):
            try:
                pcm_bytes = bytes(data.get('data'))
            except Exception:
                pcm_bytes = None
    elif isinstance(data, list):
        # Fallback: list of ints
        try:
            pcm_bytes = bytes(data)
        except Exception:
            pcm_bytes = None

    if not pcm_bytes:
        return

    # Rate-limited debug: print every ~50 chunks per sid
    try:
        if not hasattr(audio_loop, '_browser_audio_chunk_counts'):
            audio_loop._browser_audio_chunk_counts = {}
        c = audio_loop._browser_audio_chunk_counts.get(sid, 0) + 1
        audio_loop._browser_audio_chunk_counts[sid] = c
        if c % 50 == 0:
            print(f"[SERVER] mic_audio_chunk sid={sid} chunks={c} bytes={len(pcm_bytes)}")

        if str(os.getenv('BROWSER_AUDIO_DEBUG') or '').strip().lower() in ('1', 'true', 'yes'):
            if c % 100 == 0:
                try:
                    asyncio.create_task(
                        sio.emit(
                            'status',
                            {'msg': f'[SERVER] mic_audio_chunk chunks={c} bytes={len(pcm_bytes)}'},
                            room=sid,
                        )
                    )
                except Exception:
                    pass
    except Exception:
        pass

    await audio_loop.ingest_audio_chunk(pcm_bytes)


async def monitor_printers_loop():
    """Background task to query printer status periodically."""
    print("[SERVER] Starting Printer Monitor Loop")
    while audio_loop and audio_loop.printer_agent:
        try:
            agent = audio_loop.printer_agent
            if not agent.printers:
                await asyncio.sleep(5)
                continue
                
            tasks = []
            for host, printer in agent.printers.items():
                if printer.printer_type.value != "unknown":
                    tasks.append(agent.get_print_status(host))
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        pass # Ignore errors for now
                    elif res:
                        # res is PrintStatus object
                        await sio.emit('print_status_update', res.to_dict())
                        
        except asyncio.CancelledError:
            print("[SERVER] Printer Monitor Cancelled")
            break
        except Exception as e:
            print(f"[SERVER] Monitor Loop Error: {e}")
            
        await asyncio.sleep(2) # Update every 2 seconds for responsiveness

@sio.event
async def stop_audio(sid):
    global audio_loop
    print(f"[SERVER] stop_audio sid={sid}")
    try:
        await sio.emit('status', {'msg': 'stop_audio received'}, room=sid)
    except Exception:
        pass
    if audio_loop:
        audio_loop.stop() 
        print("Stopping Audio Loop")
        audio_loop = None
        await sio.emit('status', {'msg': 'A.D.A Stopped'})
        return {"ok": True}
    return {"ok": True, "status": "not_running"}

@sio.event
async def pause_audio(sid):
    global audio_loop
    if audio_loop:
        audio_loop.set_paused(True)
        print("Pausing Audio")
        await sio.emit('status', {'msg': 'Audio Paused'})

@sio.event
async def resume_audio(sid):
    global audio_loop
    if audio_loop:
        audio_loop.set_paused(False)
        print("Resuming Audio")
        await sio.emit('status', {'msg': 'Audio Resumed'})

@sio.event
async def confirm_tool(sid, data):
    # data: { "id": "...", "confirmed": True/False }
    request_id = data.get('id')
    confirmed = data.get('confirmed', False)
    
    print(f"[SERVER DEBUG] Received confirmation response for {request_id}: {confirmed}")
    
    if audio_loop:
        audio_loop.resolve_tool_confirmation(request_id, confirmed)
    else:
        print("Audio loop not active, cannot resolve confirmation.")

@sio.event
async def shutdown(sid, data=None):
    """Gracefully shutdown the server when the application closes."""
    global audio_loop, loop_task, authenticator
    
    print("[SERVER] ========================================")
    print("[SERVER] SHUTDOWN SIGNAL RECEIVED FROM FRONTEND")
    print("[SERVER] ========================================")
    
    # Stop audio loop
    if audio_loop:
        print("[SERVER] Stopping Audio Loop...")
        audio_loop.stop()
        audio_loop = None
    
    # Cancel the loop task if running
    if loop_task and not loop_task.done():
        print("[SERVER] Cancelling loop task...")
        loop_task.cancel()
        loop_task = None
    
    # Stop authenticator if running
    if authenticator:
        print("[SERVER] Stopping Authenticator...")
        authenticator.stop()
    
    print("[SERVER] Graceful shutdown complete. Terminating process...")
    
    # Force exit immediately - os._exit bypasses cleanup but ensures termination
    os._exit(0)

@sio.event
async def user_input(sid, data):
    text = data.get('text')
    print(f"[SERVER DEBUG] User input received: '{text}'")

    if isinstance(text, str) and text.strip().lower().startswith('/img '):
        prompt = text.strip()[5:].strip()
        if prompt:
            await generate_image(sid, {"prompt": prompt})
        return
    
    if not audio_loop:
        print("[SERVER DEBUG] [Error] Audio loop is None. Cannot send text.")
        return

    if not audio_loop.session:
        print("[SERVER DEBUG] [Error] Session is None. Cannot send text.")
        return

    if text:
        print(f"[SERVER DEBUG] Sending message to model: '{text}'")
        
        # Log User Input to Project History
        if audio_loop and audio_loop.project_manager:
            audio_loop.project_manager.log_chat("User", text)
            
        # Use the same 'send' method that worked for audio, as 'send_realtime_input' and 'send_client_content' seem unstable in this env
        # INJECT VIDEO FRAME IF AVAILABLE (VAD-style logic for Text Input)
        if audio_loop and audio_loop._latest_image_payload:
            print(f"[SERVER DEBUG] Piggybacking video frame with text input.")
            try:
                # Send frame first
                await audio_loop.session.send(input=audio_loop._latest_image_payload, end_of_turn=False)
            except Exception as e:
                print(f"[SERVER DEBUG] Failed to send piggyback frame: {e}")
                
        await audio_loop.session.send(input=text, end_of_turn=True)
        print(f"[SERVER DEBUG] Message sent to model successfully.")


@sio.event
async def chat_text(sid, data):
    text = (data or {}).get("text")
    if not text:
        return

    if isinstance(text, str) and text.strip().lower().startswith('/img '):
        prompt = text.strip()[5:].strip()
        if prompt:
            await generate_image(sid, {"prompt": prompt})
        return

    try:
        reply = await _call_gemini_chat(text)
    except Exception as e:
        await sio.emit('error', {'msg': f"Gemini error: {e}"}, room=sid)
        return

    await sio.emit('assistant_text', {'text': reply}, room=sid)


@sio.event
async def generate_image(sid, data):
    prompt = (data or {}).get("prompt") or ""
    prompt = prompt.strip()
    if not prompt:
        return

    global _image_rate_limit_until
    now = time.time()
    if _image_rate_limit_until and now < _image_rate_limit_until:
        wait_s = max(0, int(_image_rate_limit_until - now))
        await sio.emit(
            'error',
            {'msg': f"Image generation rate-limited. Please retry in ~{wait_s}s."},
            room=sid,
        )
        return

    reference_images = (data or {}).get("images") or []

    try:
        await sio.emit('status', {'msg': 'Generating image...'}, room=sid)
        backend = (os.getenv("IMAGE_BACKEND") or "gemini").strip().lower()
        if backend in ("mcp_imagen", "mcp-imagen", "imagen", "mcp"):
            result = await _call_mcp_imagen(prompt, reference_images=reference_images)
        else:
            result = await _call_gemini_image(prompt, reference_images=reference_images)
        image_bytes = result.get("bytes")
        mime = result.get("mime") or "image/png"
        if not image_bytes:
            raise RuntimeError("Empty image bytes")

        pm = _get_active_project_manager()
        saved_path = pm.save_image_artifact(image_bytes, prompt, mime_type=mime)
        b64 = base64.b64encode(image_bytes).decode("utf-8")

        await sio.emit(
            'image_data',
            {
                'mime': mime,
                'data': b64,
                'prompt': prompt,
                'file_path': saved_path,
                'model': (
                    (os.getenv("MCP_IMAGEN_BASE_URL") or "mcp-imagen-light")
                    if (os.getenv("IMAGE_BACKEND") or "gemini").strip().lower() in ("mcp_imagen", "mcp-imagen", "imagen", "mcp")
                    else (os.getenv("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image")
                ),
            },
            room=sid,
        )
        await sio.emit('status', {'msg': 'Image generated'}, room=sid)
    except Exception as e:
        msg = str(e)
        if "RESOURCE_EXHAUSTED" in msg or "Quota exceeded" in msg or "429" in msg:
            retry_s = None
            m = re.search(r"retryDelay['\"]?:\s*'?([0-9]+)s", msg)
            if m:
                try:
                    retry_s = int(m.group(1))
                except Exception:
                    retry_s = None
            if retry_s is None:
                m2 = re.search(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", msg, flags=re.IGNORECASE)
                if m2:
                    try:
                        retry_s = int(float(m2.group(1)))
                    except Exception:
                        retry_s = None

            if retry_s and retry_s > 0:
                _image_rate_limit_until = time.time() + retry_s

            await sio.emit(
                'error',
                {
                    'msg': (
                        "Image generation quota exceeded (Gemini API). "
                        "This usually means your API key/project has no remaining quota or billing is not enabled for the image model. "
                        + (f"Retry after ~{retry_s}s. " if retry_s else "")
                        + "See: https://ai.google.dev/gemini-api/docs/rate-limits"
                    )
                },
                room=sid,
            )
            return

        await sio.emit('error', {'msg': f"Image generation error: {e}"}, room=sid)

@sio.event
async def video_frame(sid, data):
    # data should contain 'image' which is binary (blob) or base64 encoded
    image_data = data.get('image')
    if image_data and audio_loop:
        # We don't await this because we don't want to block the socket handler
        # But send_frame is async, so we create a task
        asyncio.create_task(audio_loop.send_frame(image_data))

@sio.event
async def save_memory(sid, data):
    try:
        messages = data.get('messages', [])
        if not messages:
            print("No messages to save.")
            return

        # Ensure directory exists
        memory_dir = Path("long_term_memory")
        memory_dir.mkdir(exist_ok=True)

        # Generate filename
        # Use provided filename if available, else timestamp
        provided_name = data.get('filename')
        
        if provided_name:
            # Simple sanitization
            if not provided_name.endswith('.txt'):
                provided_name += '.txt'
            # Prevent directory traversal
            filename = memory_dir / Path(provided_name).name 
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = memory_dir / f"memory_{timestamp}.txt"

        # Write to file
        with open(filename, 'w', encoding='utf-8') as f:
            for msg in messages:
                sender = msg.get('sender', 'Unknown')
                text = msg.get('text', '')
        print(f"Conversation saved to {filename}")
        await sio.emit('status', {'msg': 'Memory Saved Successfully'})

    except Exception as e:
        print(f"Error saving memory: {e}")
        await sio.emit('error', {'msg': f"Failed to save memory: {str(e)}"})

@sio.event
async def upload_memory(sid, data):
    print(f"Received memory upload request")
    try:
        memory_text = data.get('memory', '')
        if not memory_text:
            print("No memory data provided.")
            return

        if not audio_loop:
             print("[SERVER DEBUG] [Error] Audio loop is None. Cannot load memory.")
             await sio.emit('error', {'msg': "System not ready (Audio Loop inactive)"})
             return
        
        if not audio_loop.session:
             print("[SERVER DEBUG] [Error] Session is None. Cannot load memory.")
             await sio.emit('error', {'msg': "System not ready (No active session)"})
             return

        # Send to model
        print("Sending memory context to model...")
        context_msg = f"System Notification: The user has uploaded a long-term memory file. Please load the following context into your understanding. The format is a text log of previous conversations:\n\n{memory_text}"
        
        await audio_loop.session.send(input=context_msg, end_of_turn=True)
        print("Memory context sent successfully.")
        await sio.emit('status', {'msg': 'Memory Loaded into Context'})

    except Exception as e:
        print(f"Error uploading memory: {e}")
        await sio.emit('error', {'msg': f"Failed to upload memory: {str(e)}"})

@sio.event
async def discover_kasa(sid):
    print(f"Received discover_kasa request")
    try:
        devices = await kasa_agent.discover_devices()
        await sio.emit('kasa_devices', devices)
        await sio.emit('status', {'msg': f"Found {len(devices)} Kasa devices"})
        
        # Save to settings
        # devices is a list of full device info dicts. minimizing for storage.
        saved_devices = []
        for d in devices:
            saved_devices.append({
                "ip": d["ip"],
                "alias": d["alias"],
                "model": d["model"]
            })
        
        # Merge with existing to preserve any manual overrides? 
        # For now, just overwrite with latest scan result + previously known if we want to be fancy,
        # but user asked for "Any new devices that are scanned are added there".
        # A simple full persistence of current state is safest.
        SETTINGS["kasa_devices"] = saved_devices
        save_settings()
        print(f"[SERVER] Saved {len(saved_devices)} Kasa devices to settings.")
        
    except Exception as e:
        print(f"Error discovering kasa: {e}")
        await sio.emit('error', {'msg': f"Kasa Discovery Failed: {str(e)}"})

@sio.event
async def iterate_cad(sid, data):
    # data: { prompt: "make it bigger" }
    prompt = data.get('prompt')
    print(f"Received iterate_cad request: '{prompt}'")
    
    if not audio_loop or not audio_loop.cad_agent:
        await sio.emit('error', {'msg': "CAD Agent not available"})
        return

    try:
        # Notify user work has started
        await sio.emit('status', {'msg': 'Iterating design...'})
        await sio.emit('cad_status', {'status': 'generating'})
        
        # Call the agent with project path
        cad_output_dir = str(audio_loop.project_manager.get_current_project_path() / "cad")
        result = await audio_loop.cad_agent.iterate_prototype(prompt, output_dir=cad_output_dir)
        
        if result:
            info = f"{len(result.get('data', ''))} bytes (STL)"
            print(f"Sending updated CAD data: {info}")
            await sio.emit('cad_data', result)
            # Save to Project
            if 'file_path' in result:
                saved_path = audio_loop.project_manager.save_cad_artifact(result['file_path'], prompt)
                if saved_path:
                    print(f"[SERVER] Saved iterated CAD to {saved_path}")

            await sio.emit('status', {'msg': 'Design updated'})
        else:
            await sio.emit('error', {'msg': 'Failed to update design'})
            
    except Exception as e:
        print(f"Error iterating CAD: {e}")
        await sio.emit('error', {'msg': f"Iteration Error: {str(e)}"})

@sio.event
async def generate_cad(sid, data):
    # data: { prompt: "make a cube" }
    prompt = data.get('prompt')
    print(f"Received generate_cad request: '{prompt}'")
    
    if not audio_loop or not audio_loop.cad_agent:
        await sio.emit('error', {'msg': "CAD Agent not available"})
        return

    try:
        await sio.emit('status', {'msg': 'Generating new design...'})
        await sio.emit('cad_status', {'status': 'generating'})
        
        # Use generate_prototype based on prompt with project path
        cad_output_dir = str(audio_loop.project_manager.get_current_project_path() / "cad")
        result = await audio_loop.cad_agent.generate_prototype(prompt, output_dir=cad_output_dir)
        
        if result:
            info = f"{len(result.get('data', ''))} bytes (STL)"
            print(f"Sending newly generated CAD data: {info}")
            await sio.emit('cad_data', result)


            # Save to Project
            if 'file_path' in result:
                saved_path = audio_loop.project_manager.save_cad_artifact(result['file_path'], prompt)
                if saved_path:
                    print(f"[SERVER] Saved generated CAD to {saved_path}")

            await sio.emit('status', {'msg': 'Design generated'})
        else:
            await sio.emit('error', {'msg': 'Failed to generate design'})
            
    except Exception as e:
        print(f"Error generating CAD: {e}")
        await sio.emit('error', {'msg': f"Generation Error: {str(e)}"})

@sio.event
async def prompt_web_agent(sid, data):
    # data: { prompt: "find xyz" }
    prompt = data.get('prompt')
    print(f"Received web agent prompt: '{prompt}'")
    
    if not audio_loop or not audio_loop.web_agent:
        await sio.emit('error', {'msg': "Web Agent not available"})
        return

    try:
        await sio.emit('status', {'msg': 'Web Agent running...'})
        
        # We assume web_agent has a run method or similar.
        # This might block the loop if not strictly async or offloaded.
        # Ideally web_agent.run is async.
        # And it should emit 'browser_snap' and logs automatically via hooks if setup.
        
        # We might need to launch this as a task if it's long running?
        # asyncio.create_task(audio_loop.web_agent.run(prompt))
        # But we want to catch errors here.
        
        # Based on typical agent design, run() is the entry point.
        await audio_loop.web_agent.run(prompt)
        
        await sio.emit('status', {'msg': 'Web Agent finished'})
        
    except Exception as e:
        print(f"Error running Web Agent: {e}")
        await sio.emit('error', {'msg': f"Web Agent Error: {str(e)}"})

@sio.event
async def discover_printers(sid):
    print("Received discover_printers request")
    
    # If audio_loop isn't ready yet, return saved printers from settings
    if not audio_loop or not audio_loop.printer_agent:
        saved_printers = SETTINGS.get("printers", [])
        if saved_printers:
            # Convert saved printers to the expected format
            printer_list = []
            for p in saved_printers:
                printer_list.append({
                    "name": p.get("name", p["host"]),
                    "host": p["host"],
                    "port": p.get("port", 80),
                    "printer_type": p.get("type", "unknown"),
                    "camera_url": p.get("camera_url")
                })
            print(f"[SERVER] Returning {len(printer_list)} saved printers (audio_loop not ready)")
            await sio.emit('printer_list', printer_list)
            return
        else:
            await sio.emit('printer_list', [])
            await sio.emit('status', {'msg': "Connect to A.D.A to enable printer discovery"})
            return
        
    try:
        printers = await audio_loop.printer_agent.discover_printers()
        await sio.emit('printer_list', printers)
        await sio.emit('status', {'msg': f"Found {len(printers)} printers"})
    except Exception as e:
        print(f"Error discovering printers: {e}")
        await sio.emit('error', {'msg': f"Printer Discovery Failed: {str(e)}"})

@sio.event
async def add_printer(sid, data):
    # data: { host: "192.168.1.50", name: "My Printer", type: "moonraker" }
    raw_host = data.get('host')
    name = data.get('name') or raw_host
    ptype = data.get('type', "moonraker")
    
    # Parse port if present
    if ":" in raw_host:
        host, port_str = raw_host.split(":")
        port = int(port_str)
    else:
        host = raw_host
        port = 80
    
    print(f"Received add_printer request: {host}:{port} ({ptype})")
    
    if not audio_loop or not audio_loop.printer_agent:
        await sio.emit('error', {'msg': "Printer Agent not available"})
        return
        
    try:
        # Add manually
        camera_url = data.get('camera_url')
        printer = audio_loop.printer_agent.add_printer_manually(name, host, port=port, printer_type=ptype, camera_url=camera_url)
        
        # Save to settings
        new_printer_config = {
            "name": name,
            "host": host,
            "port": port,
            "type": ptype,
            "camera_url": camera_url
        }
        
        # Check if already exists to avoid duplicates
        exists = False
        for p in SETTINGS.get("printers", []):
            if p["host"] == host and p["port"] == port:
                exists = True
                break
        
        if not exists:
            if "printers" not in SETTINGS:
                SETTINGS["printers"] = []
            SETTINGS["printers"].append(new_printer_config)
            save_settings()
            print(f"[SERVER] Saved printer {name} to settings.")
        
        # Probe to confirm/correct type
        print(f"Probing {host} to confirm type...")
        # Try port 7125 (Moonraker) and 4408 (Fluidd/K1) 
        ports_to_try = [80, 7125, 4408]
        
        actual_type = "unknown"
        for port in ports_to_try:
             found_type = await audio_loop.printer_agent._probe_printer_type(host, port)
             if found_type.value != "unknown":
                 actual_type = found_type
                 # Update port if different
                 if port != 80:
                     printer.port = port
                 break
        
        if actual_type != "unknown" and actual_type != printer.printer_type:
             printer.printer_type = actual_type
             print(f"Corrected type to {actual_type.value} on port {printer.port}")
             
        # Refresh list for everyone
        printers = [p.to_dict() for p in audio_loop.printer_agent.printers.values()]
        await sio.emit('printer_list', printers)
        await sio.emit('status', {'msg': f"Added printer: {name}"})
        
    except Exception as e:
        print(f"Error adding printer: {e}")
        await sio.emit('error', {'msg': f"Failed to add printer: {str(e)}"})

@sio.event
async def print_stl(sid, data):
    print(f"Received print_stl request: {data}")
    # data: { stl_path: "path/to.stl" | "current", printer: "name_or_ip", profile: "optional" }
    
    if not audio_loop or not audio_loop.printer_agent:
        await sio.emit('error', {'msg': "Printer Agent not available"})
        return
        
    try:
        stl_path = data.get('stl_path', 'current')
        printer_name = data.get('printer')
        profile = data.get('profile')
        
        if not printer_name:
             await sio.emit('error', {'msg': "No printer specified"})
             return
             
        await sio.emit('status', {'msg': f"Preparing print for {printer_name}..."})
        
        # Get current project path for resolution
        current_project_path = None
        if audio_loop and audio_loop.project_manager:
            current_project_path = str(audio_loop.project_manager.get_current_project_path())
            print(f"[SERVER DEBUG] Using project path: {current_project_path}")

        # Resolve STL path before slicing so we can preview it
        resolved_stl = audio_loop.printer_agent._resolve_file_path(stl_path, current_project_path)
        
        if resolved_stl and os.path.exists(resolved_stl):
            # Open the STL in the CAD module for preview
            try:
                import base64
                with open(resolved_stl, 'rb') as f:
                    stl_data = f.read()
                stl_b64 = base64.b64encode(stl_data).decode('utf-8')
                stl_filename = os.path.basename(resolved_stl)
                
                print(f"[SERVER] Opening STL in CAD module: {stl_filename}")
                await sio.emit('cad_data', {
                    'format': 'stl',
                    'data': stl_b64,
                    'filename': stl_filename
                })
            except Exception as e:
                print(f"[SERVER] Warning: Could not preview STL: {e}")
        
        # Progress Callback
        async def on_slicing_progress(percent, message):
            await sio.emit('slicing_progress', {
                'printer': printer_name,
                'percent': percent,
                'message': message
            })
            if percent < 100:
                 await sio.emit('status', {'msg': f"Slicing: {percent}%"})

        result = await audio_loop.printer_agent.print_stl(
            stl_path, 
            printer_name, 
            profile,
            progress_callback=on_slicing_progress,
            root_path=current_project_path
        )
        
        await sio.emit('print_result', result)
        await sio.emit('status', {'msg': f"Print Job: {result.get('status', 'unknown')}"})
        
    except Exception as e:
        print(f"Error printing STL: {e}")
        await sio.emit('error', {'msg': f"Print Failed: {str(e)}"})

@sio.event
async def get_slicer_profiles(sid):
    """Get available OrcaSlicer profiles for manual selection."""
    print("Received get_slicer_profiles request")
    if not audio_loop or not audio_loop.printer_agent:
        await sio.emit('error', {'msg': "Printer Agent not available"})
        return
    
    try:
        profiles = audio_loop.printer_agent.get_available_profiles()
        await sio.emit('slicer_profiles', profiles)
    except Exception as e:
        print(f"Error getting slicer profiles: {e}")
        await sio.emit('error', {'msg': f"Failed to get profiles: {str(e)}"})

@sio.event
async def control_kasa(sid, data):
    # data: { ip, action: "on"|"off"|"brightness"|"color", value: ... }
    ip = data.get('ip')
    action = data.get('action')
    print(f"Kasa Control: {ip} -> {action}")
    
    try:
        success = False
        if action == "on":
            success = await kasa_agent.turn_on(ip)
        elif action == "off":
            success = await kasa_agent.turn_off(ip)
        elif action == "brightness":
            val = data.get('value')
            success = await kasa_agent.set_brightness(ip, val)
        elif action == "color":
            # value is {h, s, v} - convert to tuple for set_color
            h = data.get('value', {}).get('h', 0)
            s = data.get('value', {}).get('s', 100)
            v = data.get('value', {}).get('v', 100)
            success = await kasa_agent.set_color(ip, (h, s, v))
        
        if success:
            await sio.emit('kasa_update', {
                'ip': ip,
                'is_on': True if action == "on" else (False if action == "off" else None),
                'brightness': data.get('value') if action == "brightness" else None,
            })
 
        else:
             await sio.emit('error', {'msg': f"Failed to control device {ip}"})

    except Exception as e:
         print(f"Error controlling kasa: {e}")
         await sio.emit('error', {'msg': f"Kasa Control Error: {str(e)}"})

@sio.event
async def get_settings(sid):
    await sio.emit('settings', SETTINGS)

@sio.event
async def update_settings(sid, data):
    print(f"Updating settings: {data}")
    
    # Handle specific keys if needed
    if "tool_permissions" in data:
        SETTINGS["tool_permissions"].update(data["tool_permissions"])
        _enforce_tool_permission_defaults()
        if audio_loop:
            audio_loop.update_permissions(SETTINGS["tool_permissions"])
            
    if "face_auth_enabled" in data:
        SETTINGS["face_auth_enabled"] = data["face_auth_enabled"]
        # If turned OFF, maybe emit auth status true?
        if not data["face_auth_enabled"]:
             await sio.emit('auth_status', {'authenticated': True})
             # Stop auth loop if running?
             if authenticator:
                 authenticator.stop() 

    if "camera_flipped" in data:
        SETTINGS["camera_flipped"] = data["camera_flipped"]
        print(f"[SERVER] Camera flip set to: {data['camera_flipped']}")

    save_settings()
    # Broadcast new full settings
    await sio.emit('settings', SETTINGS)


# Deprecated/Mapped for compatibility if frontend still uses specific events
@sio.event
async def get_tool_permissions(sid):
    await sio.emit('tool_permissions', SETTINGS["tool_permissions"])

@sio.event
async def update_tool_permissions(sid, data):
    print(f"Updating permissions (legacy event): {data}")
    SETTINGS["tool_permissions"].update(data)
    save_settings()
    
    if audio_loop:
        audio_loop.update_permissions(SETTINGS["tool_permissions"])
    # Broadcast update to all
    await sio.emit('tool_permissions', SETTINGS["tool_permissions"])

if __name__ == "__main__":
    uvicorn.run(
        "server:app_socketio", 
        host="127.0.0.1", 
        port=8000, 
        reload=False, # Reload enabled causes spawn of worker which might miss the event loop policy patch
        loop="asyncio",
        reload_excludes=["temp_cad_gen.py", "output.stl", "*.stl"]
    )
