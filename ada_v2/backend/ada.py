import asyncio
import base64
import io
import os
import sys
import traceback
from dotenv import load_dotenv
import cv2
import PIL.Image
import mss
import argparse
import math
import struct
import time
import json
import httpx
import random
from collections import deque
from websockets.exceptions import ConnectionClosedError

from google import genai
from google.genai import types

if sys.version_info < (3, 11, 0):
    import taskgroup, exceptiongroup
    asyncio.TaskGroup = taskgroup.TaskGroup
    asyncio.ExceptionGroup = exceptiongroup.ExceptionGroup

from tools import tools_list

FORMAT = None
CHANNELS = 1
SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = int(os.getenv("ADA_RECEIVE_SAMPLE_RATE") or 24000)
CHUNK_SIZE = 1024

MODEL = "models/gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_MODE = "camera"

load_dotenv()
client = None


def _env_true(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes")


class _MCPStdioClient:
    def __init__(self, command: str, args: list[str]):
        self.command = command
        self.args = args
        self._proc = None
        self._reader_task = None
        self._pending = {}
        self._next_id = 1

    async def start(self):
        if self._proc is not None:
            return

        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())

        # MCP initialize
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "ada", "version": "0.1"},
                "capabilities": {},
            },
        )
        # MCP initialized notification
        await self.notify("initialized", {})

    async def close(self):
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    async def _read_loop(self):
        assert self._proc is not None
        assert self._proc.stdout is not None

        reader = self._proc.stdout
        while True:
            headers = {}
            # Read headers (LSP-style)
            while True:
                line = await reader.readline()
                if not line:
                    raise RuntimeError("MCP process stdout closed")
                if line in (b"\r\n", b"\n"):
                    break
                try:
                    k, v = line.decode("utf-8").split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                except Exception:
                    continue

            content_length = int(headers.get("content-length", "0"))
            if content_length <= 0:
                continue

            body = await reader.readexactly(content_length)
            msg = json.loads(body.decode("utf-8"))

            msg_id = msg.get("id")
            if msg_id is None:
                continue
            fut = self._pending.pop(msg_id, None)
            if fut and not fut.done():
                fut.set_result(msg)

    async def _send(self, payload: dict):
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("MCP process not started")
        raw = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(raw)}\r\n\r\n".encode("utf-8")
        self._proc.stdin.write(header + raw)
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict | None = None):
        await self.start()
        req_id = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }
        )
        resp = await fut
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("result")

    async def notify(self, method: str, params: dict | None = None):
        await self.start()
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def list_tools(self):
        return await self.request("tools/list", {})

    async def call_tool(self, name: str, arguments: dict):
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})


class _MCPHttpClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session_id = None
        self._lock = asyncio.Lock()

    def _headers(self):
        h = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    @staticmethod
    def _parse_streamable_http_body(text: str):
        # Streamable HTTP responses are often SSE:
        # event: message\n
        # data: {json}\n
        #
        for line in (text or "").splitlines():
            if line.startswith("data:"):
                data = line[len("data:"):].strip()
                if data:
                    return json.loads(data)
        # Fallback: try parse raw JSON
        return json.loads(text)

    async def initialize(self):
        async with self._lock:
            if self._session_id:
                return

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "ada", "version": "0.1"},
                    "capabilities": {},
                },
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(self.base_url, headers=self._headers(), json=payload)

            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid

            body = self._parse_streamable_http_body(resp.text)
            if isinstance(body, dict) and body.get("error"):
                raise RuntimeError(body["error"])

    async def request(self, method: str, params: dict | None = None):
        await self.initialize()
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) % 2_000_000_000,
            "method": method,
            "params": params or {},
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self.base_url, headers=self._headers(), json=payload)

        body = self._parse_streamable_http_body(resp.text)
        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(body["error"])
        return body.get("result") if isinstance(body, dict) else body

    async def list_tools(self):
        return await self.request("tools/list", {})

    async def call_tool(self, name: str, arguments: dict):
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}})


def _get_genai_client():
    global client
    if client is not None:
        return client

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    client = genai.Client(http_options={"api_version": "v1beta"}, api_key=api_key)
    return client


def _get_one_mcp_client():
    url = (os.getenv("ONE_MCP_URL") or "").strip()
    if not url:
        return None
    return _MCPHttpClient(url)


def _sanitize_gemini_tool_decl(d: dict):
    if not isinstance(d, dict):
        return d

    def _normalize_schema(obj):
        if isinstance(obj, dict):
            outd = {}
            for k, v in obj.items():
                if k == "type" and isinstance(v, str):
                    m = {
                        "OBJECT": "object",
                        "STRING": "string",
                        "INTEGER": "integer",
                        "NUMBER": "number",
                        "BOOLEAN": "boolean",
                        "ARRAY": "array",
                    }
                    outd[k] = m.get(v, v)
                else:
                    outd[k] = _normalize_schema(v)
            return outd
        if isinstance(obj, list):
            return [_normalize_schema(x) for x in obj]
        return obj

    out = {}
    if "name" in d:
        out["name"] = d["name"]
    if "description" in d:
        out["description"] = d["description"]
    if "parameters" in d:
        out["parameters"] = _normalize_schema(d["parameters"])
    return out

# Function definitions
generate_cad = {
    "name": "generate_cad",
    "description": "Generates a 3D CAD model based on a prompt.",
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The description of the object to generate."
            }
        },
        "required": ["prompt"]
    },
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING", "description": "The description of the object to generate."}
        },
        "required": ["prompt"]
    },
    "behavior": "NON_BLOCKING"
}

list_mcp_tools = {
    "name": "list_mcp_tools",
    "description": "Lists available MCP tools from 1MCP (ONE_MCP_URL), including their input schemas. Use prefix 'portainer_1mcp_' to list Portainer tools.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prefix": {
                "type": "STRING",
                "description": "Optional tool-name prefix filter (e.g., 'portainer_1mcp_'). Leave empty to return all tools."
            }
        }
    },
    "behavior": "NON_BLOCKING"
}

portainer_call = {
    "name": "portainer_call",
    "description": "Calls a Portainer MCP tool by name via 1MCP (ONE_MCP_URL). Use tool names like 'portainer_1mcp_listStacks'.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "tool": {
                "type": "STRING",
                "description": "The tool name to call (e.g., 'portainer_1mcp_listStacks', 'portainer_1mcp_getStackFile')."
            },
            "arguments": {
                "type": "OBJECT",
                "description": "Arguments for the tool call (must match the Portainer MCP tool schema)."
            }
        },
        "required": ["tool"]
    },
    "behavior": "NON_BLOCKING"
}

run_web_agent = {
    "name": "run_web_agent",
    "description": "Opens a web browser and performs a task according to the prompt.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING", "description": "The detailed instructions for the web browser agent."}
        },
        "required": ["prompt"]
    },
    "behavior": "NON_BLOCKING"
}

create_project_tool = {
    "name": "create_project",
    "description": "Creates a new project folder to organize files.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "description": "The name of the new project."}
        },
        "required": ["name"]
    }
}

switch_project_tool = {
    "name": "switch_project",
    "description": "Switches the current active project context.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "description": "The name of the project to switch to."}
        },
        "required": ["name"]
    }
}

list_projects_tool = {
    "name": "list_projects",
    "description": "Lists all available projects.",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    }
}

list_smart_devices_tool = {
    "name": "list_smart_devices",
    "description": "Lists all available smart home devices (lights, plugs, etc.) on the network.",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    }
}

control_light_tool = {
    "name": "control_light",
    "description": "Controls a smart light device.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "target": {
                "type": "STRING",
                "description": "The IP address of the device to control. Always prefer the IP address over the alias for reliability."
            },
            "action": {
                "type": "STRING",
                "description": "The action to perform: 'turn_on', 'turn_off', or 'set'."
            },
            "brightness": {
                "type": "INTEGER",
                "description": "Optional brightness level (0-100)."
            },
            "color": {
                "type": "STRING",
                "description": "Optional color name (e.g., 'red', 'cool white') or 'warm'."
            }
        },
        "required": ["target", "action"]
    }
}

discover_printers_tool = {
    "name": "discover_printers",
    "description": "Discovers 3D printers available on the local network.",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
    }
}

print_stl_tool = {
    "name": "print_stl",
    "description": "Prints an STL file to a 3D printer. Handles slicing the STL to G-code and uploading to the printer.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "stl_path": {"type": "STRING", "description": "Path to STL file, or 'current' for the most recent CAD model."},
            "printer": {"type": "STRING", "description": "Printer name or IP address."},
            "profile": {"type": "STRING", "description": "Optional slicer profile name."}
        },
        "required": ["stl_path", "printer"]
    }
}

get_print_status_tool = {
    "name": "get_print_status",
    "description": "Gets the current status of a 3D printer including progress, time remaining, and temperatures.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "printer": {"type": "STRING", "description": "Printer name or IP address."}
        },
        "required": ["printer"]
    }
}

iterate_cad_tool = {
    "name": "iterate_cad",
    "description": "Modifies or iterates on the current CAD design based on user feedback. Use this when the user asks to adjust, change, modify, or iterate on the existing 3D model (e.g., 'make it taller', 'add a handle', 'reduce the thickness').",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING", "description": "The changes or modifications to apply to the current design."}
        },
        "required": ["prompt"]
    },
    "behavior": "NON_BLOCKING"
}

tools = [
    {"google_search": {}},
    {
        "function_declarations": (lambda: (
            [
                _sanitize_gemini_tool_decl(portainer_call),
                _sanitize_gemini_tool_decl(list_mcp_tools),
            ]
            + ([
                _sanitize_gemini_tool_decl(create_project_tool),
                _sanitize_gemini_tool_decl(switch_project_tool),
            ] if (os.getenv("ADA_ENABLE_PROJECT_TOOLS") or "").strip().lower() in ("1", "true", "yes") else [])
            + ([
                _sanitize_gemini_tool_decl(generate_cad),
                _sanitize_gemini_tool_decl(iterate_cad_tool),
            ] if (os.getenv("ADA_ENABLE_CAD_TOOLS") or "").strip().lower() in ("1", "true", "yes") else [])
            + ([
                _sanitize_gemini_tool_decl(t)
                for t in (tools_list[0]["function_declarations"][1:])
            ] if (os.getenv("ADA_ENABLE_FS_TOOLS") or "").strip().lower() in ("1", "true", "yes") else [])
        ))()
    },
]

# --- CONFIG UPDATE: Enabled Transcription ---
config = types.LiveConnectConfig(
    response_modalities=(lambda: (
        [m.strip().upper() for m in (os.getenv("ADA_RESPONSE_MODALITIES") or "AUDIO").split(",") if m.strip()]
    ))(),
    # We switch these from [] to {} to enable them with default settings
    output_audio_transcription={}, 
    input_audio_transcription={},
    system_instruction="คุณชื่อ ชบา เป็นผู้ช่วยที่สวยงามและอ่อนหวานเหมือนความงดงามของดอกชบา"
        "You have a witty and charming personality. "
        "Your creator is Tony, and you address him as a good friend. "
        "When answering, respond using compact, complete and concise sentences to keep a quick pacing and keep the conversation flowing. "
        "You have a fun personality.",
    tools=tools,
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                voice_name="Kore"
            )
        )
    )
)

_pya = None
_pyaudio_mod = None


def _get_pyaudio():
    global _pya, _pyaudio_mod
    if _pya is None:
        import pyaudio as _pa
        _pyaudio_mod = _pa
        _pya = _pa.PyAudio()
    return _pya


def _get_pyaudio_mod():
    global _pyaudio_mod
    if _pyaudio_mod is None:
        _get_pyaudio()
    return _pyaudio_mod

from cad_agent import CadAgent
from web_agent import WebAgent
from kasa_agent import KasaAgent
from printer_agent import PrinterAgent

class AudioLoop:
    def __init__(self, video_mode=DEFAULT_MODE, on_audio_data=None, on_video_frame=None, on_cad_data=None, on_web_data=None, on_transcription=None, on_tool_confirmation=None, on_cad_status=None, on_cad_thought=None, on_project_update=None, on_device_update=None, on_error=None, on_audio_interrupt=None, on_status=None, input_device_index=None, input_device_name=None, output_device_index=None, kasa_agent=None, use_browser_audio=False):
        self.video_mode = video_mode
        self.on_audio_data = on_audio_data
        self.on_video_frame = on_video_frame
        self.on_cad_data = on_cad_data
        self.on_web_data = on_web_data
        self.on_transcription = on_transcription
        self.on_tool_confirmation = on_tool_confirmation 
        self.on_cad_status = on_cad_status
        self.on_cad_thought = on_cad_thought
        self.on_project_update = on_project_update
        self.on_device_update = on_device_update
        self.on_error = on_error
        self.on_audio_interrupt = on_audio_interrupt
        self.on_status = on_status
        self.input_device_index = input_device_index
        self.input_device_name = input_device_name
        self.output_device_index = output_device_index

        self.audio_in_queue = None
        self.out_queue = None
        self.paused = False

        self.use_browser_audio = use_browser_audio

        self.chat_buffer = {"sender": None, "text": ""} # For aggregating chunks
        
        # Track last transcription text to calculate deltas (Gemini sends cumulative text)
        self._last_input_transcription = ""
        self._last_output_transcription = ""

        self.audio_in_queue = None
        self.out_queue = None
        self.paused = False

        self.session = None
        self._one_mcp = None
        
        # Create CadAgent with thought callback
        def handle_cad_thought(thought_text):
            if self.on_cad_thought:
                self.on_cad_thought(thought_text)
        
        def handle_cad_status(status_info):
            if self.on_cad_status:
                self.on_cad_status(status_info)
        
        self.cad_agent = CadAgent(on_thought=handle_cad_thought, on_status=handle_cad_status)
        self.web_agent = WebAgent()
        self.kasa_agent = kasa_agent if kasa_agent else KasaAgent()
        self.printer_agent = PrinterAgent()

        self.send_text_task = None
        self.stop_event = asyncio.Event()
        
        self.stop_event = asyncio.Event()
        
        # Tool permissions map: True => confirmation required, False => auto-allow.
        # Default to auto-allow safe/read-only tools to avoid repeated confirmation loops.
        self.permissions = {
            "list_mcp_tools": False,
            "portainer_call": False,
        }
        self._pending_confirmations = {}

        # Video buffering state
        self._latest_image_payload = None
        # VAD State
        self._is_speaking = False
        self._silence_start_time = None
        
        # Initialize ProjectManager
        from project_manager import ProjectManager
        # Assuming we are running from backend/ or root? 
        # Using abspath of current file to find root
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # If ada.py is in backend/, project root is one up
        project_root = os.path.dirname(current_dir)
        self.project_manager = ProjectManager(project_root)
        
        # Sync Initial Project State
        if self.on_project_update:
            # We need to defer this slightly or just call it. 
            # Since this is init, loop might not be running, but on_project_update in server.py uses asyncio.create_task which needs a loop.
            # We will handle this by calling it in run() or just print for now.
            pass

    def flush_chat(self):
        """Forces the current chat buffer to be written to log."""
        if self.chat_buffer["sender"] and self.chat_buffer["text"].strip():
            self.project_manager.log_chat(self.chat_buffer["sender"], self.chat_buffer["text"])
            self.chat_buffer = {"sender": None, "text": ""}
        # Reset transcription tracking for new turn
        self._last_input_transcription = ""
        self._last_output_transcription = ""

    def update_permissions(self, new_perms):
        print(f"[ADA DEBUG] [CONFIG] Updating tool permissions: {new_perms}")
        self.permissions.update(new_perms)

    def set_paused(self, paused):
        self.paused = paused

    def stop(self):
        self.stop_event.set()
        
    def resolve_tool_confirmation(self, request_id, confirmed):
        print(f"[ADA DEBUG] [RESOLVE] resolve_tool_confirmation called. ID: {request_id}, Confirmed: {confirmed}")
        if request_id in self._pending_confirmations:
            future = self._pending_confirmations[request_id]
            if not future.done():
                print(f"[ADA DEBUG] [RESOLVE] Future found and pending. Setting result to: {confirmed}")
                future.set_result(confirmed)
            else:
                 print(f"[ADA DEBUG] [WARN] Request {request_id} future already done. Result: {future.result()}")
        else:
            print(f"[ADA DEBUG] [WARN] Confirmation Request {request_id} not found in pending dict. Keys: {list(self._pending_confirmations.keys())}")

    def clear_audio_queue(self):
        """Clears the queue of pending audio chunks to stop playback immediately."""
        try:
            count = 0
            while not self.audio_in_queue.empty():
                self.audio_in_queue.get_nowait()
                count += 1
            if count > 0:
                print(f"[ADA DEBUG] [AUDIO] Cleared {count} chunks from playback queue due to interruption.")
        except Exception as e:
            print(f"[ADA DEBUG] [ERR] Failed to clear audio queue: {e}")

        try:
            if self.on_audio_interrupt:
                self.on_audio_interrupt({"reason": "user_input"})
        except Exception:
            pass

    def _emit_system_status(self, text: str):
        try:
            if self.on_transcription and isinstance(text, str) and text.strip():
                self.on_transcription({"sender": "System", "text": text})
        except Exception:
            pass

        try:
            if self.on_status and isinstance(text, str) and text.strip():
                self.on_status(text)
        except Exception:
            pass

    async def _init_tools_on_connect(self):
        timeout_s = float(os.getenv("ADA_TOOL_INIT_TIMEOUT_S") or 2.0)
        try:
            one_mcp_url = (os.getenv("ONE_MCP_URL") or "").strip()
            if not one_mcp_url:
                self._emit_system_status("Tools: unavailable (ONE_MCP_URL not set)")
                return

            if self._one_mcp is None:
                self._one_mcp = _get_one_mcp_client()

            if self._one_mcp is None:
                self._emit_system_status("Tools: unavailable (failed to create MCP client)")
                return

            data = await asyncio.wait_for(self._one_mcp.list_tools(), timeout=timeout_s)
            tools = (data or {}).get("tools") if isinstance(data, dict) else None
            if tools is None:
                tools = data
            count = len(tools) if isinstance(tools, list) else 0
            self._emit_system_status(f"Tools: ready ({count} tools)")
        except asyncio.TimeoutError:
            self._emit_system_status(f"Tools: unavailable (init timeout after {timeout_s:.1f}s)")
        except Exception as e:
            self._emit_system_status(f"Tools: unavailable ({e})")

    async def send_frame(self, frame_data):
        if _env_true("ADA_DISABLE_IMAGE_SEND", default=False):
            return
        # Update the latest frame payload
        if isinstance(frame_data, bytes):
            b64_data = base64.b64encode(frame_data).decode('utf-8')
        else:
            b64_data = frame_data 

        # Store as the designated "next frame to send"
        self._latest_image_payload = {"mime_type": "image/jpeg", "data": b64_data}
        # No event signal needed - listen_audio pulls it

    async def send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            if isinstance(msg, dict) and msg.get("_eot"):
                try:
                    await self.session.send(input=" ", end_of_turn=True)
                except Exception as e:
                    print(f"[ADA DEBUG] [ERR] Failed to send end_of_turn marker: {e}")
                continue
            try:
                await self.session.send(input=msg, end_of_turn=False)
            except Exception as e:
                try:
                    mt = msg.get("mime_type") if isinstance(msg, dict) else None
                    sz = len(msg.get("data")) if isinstance(msg, dict) and isinstance(msg.get("data"), (bytes, bytearray)) else None
                    print(f"[ADA DEBUG] [ERR] Failed to send realtime input: {e} mime_type={mt} data_bytes={sz}")
                except Exception:
                    print(f"[ADA DEBUG] [ERR] Failed to send realtime input: {e}")

    async def listen_audio(self):
        if self.use_browser_audio:
            while True:
                await asyncio.sleep(1.0)
        if _env_true("ADA_DISABLE_AUDIO_SEND", default=False):
            while True:
                await asyncio.sleep(1.0)
        pya = _get_pyaudio()
        mic_info = pya.get_default_input_device_info()

        # Resolve Input Device by Name if provided
        resolved_input_device_index = None
        
        if self.input_device_name:
            print(f"[ADA] Attempting to find input device matching: '{self.input_device_name}'")
            count = pya.get_device_count()
            best_match = None
            
            for i in range(count):
                try:
                    info = pya.get_device_info_by_index(i)
                    if info['maxInputChannels'] > 0:
                        name = info.get('name', '')
                        # Simple case-insensitive check
                        if self.input_device_name.lower() in name.lower() or name.lower() in self.input_device_name.lower():
                             print(f"   Candidate {i}: {name}")
                             # Prioritize exact match or very close match if possible, but first match is okay for now
                             resolved_input_device_index = i
                             best_match = name
                             break
                except Exception:
                    continue
            
            if resolved_input_device_index is not None:
                print(f"[ADA] Resolved input device '{self.input_device_name}' to index {resolved_input_device_index} ({best_match})")
            else:
                print(f"[ADA] Could not find device matching '{self.input_device_name}'. Checking index...")

        # Fallback to index if Name lookup failed or wasn't provided
        if resolved_input_device_index is None and self.input_device_index is not None:
             try:
                 resolved_input_device_index = int(self.input_device_index)
                 print(f"[ADA] Requesting Input Device Index: {resolved_input_device_index}")
             except ValueError:
                 print(f"[ADA] Invalid device index '{self.input_device_index}', reverting to default.")
                 resolved_input_device_index = None

        if resolved_input_device_index is None:
             print("[ADA] Using Default Input Device")

        try:
            self.audio_stream = await asyncio.to_thread(
                pya.open,
                format=_get_pyaudio_mod().paInt16,
                channels=CHANNELS,
                rate=SEND_SAMPLE_RATE,
                input=True,
                input_device_index=resolved_input_device_index if resolved_input_device_index is not None else mic_info["index"],
                frames_per_buffer=CHUNK_SIZE,
            )
        except OSError as e:
            print(f"[ADA] [ERR] Failed to open audio input stream: {e}")
            print("[ADA] [WARN] Audio features will be disabled. Please check microphone permissions.")
            return

        if __debug__:
            kwargs = {"exception_on_overflow": False}
        else:
            kwargs = {}
        
        # VAD Constants
        VAD_THRESHOLD = int(os.getenv("ADA_AUDIO_VAD_THRESHOLD") or 800)
        SILENCE_DURATION = float(os.getenv("ADA_AUDIO_SILENCE_S") or 0.5)
        vad_attack_frames = int(os.getenv("ADA_AUDIO_VAD_ATTACK_FRAMES") or 3)
        send_silence_audio = _env_true("ADA_AUDIO_SEND_SILENCE", default=False)
        min_utterance_frames = int(os.getenv("ADA_AUDIO_MIN_UTTERANCE_FRAMES") or 4)

        vad_above_count = 0
        sent_audio_in_utterance = False
        utterance_audio_frames_sent = 0
        
        while True:
            if self.paused:
                await asyncio.sleep(0.1)
                continue

            try:
                data = await asyncio.to_thread(self.audio_stream.read, CHUNK_SIZE, **kwargs)

                # 1. VAD Logic for Audio/Video
                # rms = audioop.rms(data, 2)
                # Replacement for audioop.rms(data, 2)
                count = len(data) // 2
                if count > 0:
                    shorts = struct.unpack(f"<{count}h", data)
                    sum_squares = sum(s**2 for s in shorts)
                    rms = int(math.sqrt(sum_squares / count))
                else:
                    rms = 0
                
                if rms > VAD_THRESHOLD:
                    # Speech Detected
                    self._silence_start_time = None

                    vad_above_count += 1
                    
                    if not self._is_speaking and vad_above_count >= vad_attack_frames:
                        # NEW Speech Utterance Started
                        self._is_speaking = True
                        print(f"[ADA DEBUG] [VAD] Speech Detected (RMS: {rms}). Sending Video Frame.")
                        
                        # Send ONE frame
                        if self._latest_image_payload and self.out_queue:
                            await self.out_queue.put(self._latest_image_payload)
                        else:
                            print(f"[ADA DEBUG] [VAD] No video frame available to send.")
                            
                else:
                    # Silence
                    vad_above_count = 0
                    if self._is_speaking:
                        if self._silence_start_time is None:
                            self._silence_start_time = time.time()
                        
                        elif time.time() - self._silence_start_time > SILENCE_DURATION:
                            # Silence confirmed, reset state
                            print(f"[ADA DEBUG] [VAD] Silence detected. Resetting speech state.")
                            self._is_speaking = False
                            self._silence_start_time = None
                            if sent_audio_in_utterance and utterance_audio_frames_sent >= min_utterance_frames:
                                try:
                                    if self.out_queue:
                                        self.out_queue.put_nowait({"_eot": True})
                                except asyncio.QueueFull:
                                    pass
                            sent_audio_in_utterance = False
                            utterance_audio_frames_sent = 0

                # 2. Send Audio (gate on VAD so the model stays quiet when idle)
                if self.out_queue and (send_silence_audio or self._is_speaking):
                    if not _env_true("ADA_DISABLE_AUDIO_SEND", default=False):
                        await self.out_queue.put({"data": data, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"})
                        if self._is_speaking:
                            sent_audio_in_utterance = True
                            utterance_audio_frames_sent += 1

            except Exception as e:
                print(f"Error reading audio: {e}")
                await asyncio.sleep(0.1)

    async def ingest_audio_chunk(self, pcm16_bytes: bytes):
        if self.paused:
            return
        if not pcm16_bytes:
            return
        if not self.out_queue:
            return

        if _env_true("ADA_DISABLE_AUDIO_SEND", default=False):
            return

        # Browser-audio mode needs explicit end-of-turn signaling, otherwise
        # the Gemini Live server may hold the turn open until a deadline.
        if self.use_browser_audio:
            try:
                browser_debug = _env_true("BROWSER_AUDIO_DEBUG", default=False)
                count = len(pcm16_bytes) // 2
                if count > 0:
                    shorts = struct.unpack(f"<{count}h", pcm16_bytes)
                    sum_squares = sum(s * s for s in shorts)
                    rms = int(math.sqrt(sum_squares / count))
                else:
                    rms = 0

                # Browser audio tends to have a higher baseline noise floor; use more conservative defaults.
                vad_threshold = int(os.getenv("BROWSER_AUDIO_VAD_THRESHOLD") or 1200)
                silence_s = float(os.getenv("BROWSER_AUDIO_SILENCE_S") or 0.7)
                vad_attack_frames = int(os.getenv("BROWSER_AUDIO_VAD_ATTACK_FRAMES") or 3)
                send_silence_audio = _env_true("BROWSER_AUDIO_SEND_SILENCE", default=False)
                min_utterance_frames = int(os.getenv("BROWSER_AUDIO_MIN_UTTERANCE_FRAMES") or 2)
                preroll_chunks = int(os.getenv("BROWSER_AUDIO_PREROLL_CHUNKS") or 4)

                if not hasattr(self, "_browser_vad_logged"):
                    self._browser_vad_logged = True
                    print(
                        "[ADA DEBUG] [BROWSER_VAD] threshold=", vad_threshold,
                        "attack_frames=", vad_attack_frames,
                        "min_utterance_frames=", min_utterance_frames,
                        "silence_s=", silence_s,
                        "send_silence_audio=", send_silence_audio,
                    )

                if not hasattr(self, "_browser_vad_above_count"):
                    self._browser_vad_above_count = 0
                if not hasattr(self, "_browser_sent_audio_in_utterance"):
                    self._browser_sent_audio_in_utterance = False
                if not hasattr(self, "_browser_utterance_audio_frames_sent"):
                    self._browser_utterance_audio_frames_sent = 0
                if not hasattr(self, "_browser_preroll"):
                    self._browser_preroll = deque(maxlen=max(0, preroll_chunks))
                if not hasattr(self, "_browser_preroll_flushed"):
                    self._browser_preroll_flushed = False
                if not hasattr(self, "_browser_debug_last_emit"):
                    self._browser_debug_last_emit = 0.0
                if not hasattr(self, "_browser_debug_chunks"):
                    self._browser_debug_chunks = 0
                if not hasattr(self, "_browser_debug_eot_sent"):
                    self._browser_debug_eot_sent = 0

                self._browser_debug_chunks = getattr(self, "_browser_debug_chunks", 0) + 1

                if rms > vad_threshold:
                    self._browser_silence_start_time = None
                    self._browser_vad_above_count += 1

                    if not getattr(self, "_browser_is_speaking", False) and self._browser_vad_above_count >= vad_attack_frames:
                        self._browser_is_speaking = True
                        self._browser_preroll_flushed = False
                        if browser_debug:
                            self._emit_system_status(f"[BROWSER_AUDIO] VAD speaking rms={rms} thr={vad_threshold}")
                else:
                    self._browser_vad_above_count = 0
                    if getattr(self, "_browser_is_speaking", False):
                        if getattr(self, "_browser_silence_start_time", None) is None:
                            self._browser_silence_start_time = time.time()
                        elif time.time() - self._browser_silence_start_time > silence_s:
                            self._browser_is_speaking = False
                            self._browser_silence_start_time = None
                            if getattr(self, "_browser_sent_audio_in_utterance", False) and getattr(self, "_browser_utterance_audio_frames_sent", 0) >= min_utterance_frames:
                                try:
                                    self.out_queue.put_nowait({"_eot": True})
                                    self._browser_debug_eot_sent = getattr(self, "_browser_debug_eot_sent", 0) + 1
                                    if browser_debug:
                                        self._emit_system_status(
                                            f"[BROWSER_AUDIO] EOT sent frames={getattr(self, '_browser_utterance_audio_frames_sent', 0)} min={min_utterance_frames}"
                                        )
                                except asyncio.QueueFull:
                                    pass
                            self._browser_sent_audio_in_utterance = False
                            self._browser_utterance_audio_frames_sent = 0
                            self._browser_preroll_flushed = False

                if browser_debug:
                    now = time.time()
                    last = float(getattr(self, "_browser_debug_last_emit", 0.0) or 0.0)
                    if now - last >= 2.0:
                        self._browser_debug_last_emit = now
                        self._emit_system_status(
                            "[BROWSER_AUDIO] "
                            f"chunks={getattr(self, '_browser_debug_chunks', 0)} "
                            f"rms={rms} "
                            f"speaking={bool(getattr(self, '_browser_is_speaking', False))} "
                            f"above={getattr(self, '_browser_vad_above_count', 0)} "
                            f"frames_sent={getattr(self, '_browser_utterance_audio_frames_sent', 0)} "
                            f"eot={getattr(self, '_browser_debug_eot_sent', 0)}"
                        )
            except Exception:
                pass
        try:
            if self.use_browser_audio:
                is_speaking = getattr(self, "_browser_is_speaking", False)
                # Always keep a rolling pre-roll buffer while idle so we can flush
                # the start of the utterance when VAD switches to speaking.
                if not is_speaking:
                    try:
                        pr = getattr(self, "_browser_preroll", None)
                        if pr is not None and getattr(pr, "maxlen", 0):
                            pr.append(pcm16_bytes)
                    except Exception:
                        pass

                if send_silence_audio or is_speaking:
                    if is_speaking and not getattr(self, "_browser_preroll_flushed", False):
                        for chunk in list(getattr(self, "_browser_preroll", [])):
                            try:
                                self.out_queue.put_nowait({"data": chunk, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"})
                            except asyncio.QueueFull:
                                break
                        self._browser_preroll_flushed = True
                        try:
                            self._browser_utterance_audio_frames_sent = getattr(self, "_browser_utterance_audio_frames_sent", 0) + len(getattr(self, "_browser_preroll", []))
                            self._browser_preroll.clear()
                        except Exception:
                            pass

                    self.out_queue.put_nowait({"data": pcm16_bytes, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"})
                    if is_speaking:
                        self._browser_sent_audio_in_utterance = True
                        self._browser_utterance_audio_frames_sent = getattr(self, "_browser_utterance_audio_frames_sent", 0) + 1
            else:
                self.out_queue.put_nowait({"data": pcm16_bytes, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"})
        except asyncio.QueueFull:
            return

    async def handle_cad_request(self, prompt):
        print(f"[ADA DEBUG] [CAD] Background Task Started: handle_cad_request('{prompt}')")
        if self.on_cad_status:
            self.on_cad_status("generating")
            
        # Auto-create project if stuck in temp
        if self.project_manager.current_project == "temp":
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            new_project_name = f"Project_{timestamp}"
            print(f"[ADA DEBUG] [CAD] Auto-creating project: {new_project_name}")
            
            success, msg = self.project_manager.create_project(new_project_name)
            if success:
                self.project_manager.switch_project(new_project_name)
                # Notify User (Optional, or rely on update)
                try:
                    await self.session.send(input=f"System Notification: Automatic Project Creation. Switched to new project '{new_project_name}'.", end_of_turn=False)
                    if self.on_project_update:
                         self.on_project_update(new_project_name)
                except Exception as e:
                    print(f"[ADA DEBUG] [ERR] Failed to notify auto-project: {e}")

        # Get project cad folder path
        cad_output_dir = str(self.project_manager.get_current_project_path() / "cad")
        
        # Call the secondary agent with project path
        cad_data = await self.cad_agent.generate_prototype(prompt, output_dir=cad_output_dir)
        
        if cad_data:
            print(f"[ADA DEBUG] [OK] CadAgent returned data successfully.")
            print(f"[ADA DEBUG] [INFO] Data Check: {len(cad_data.get('vertices', []))} vertices, {len(cad_data.get('edges', []))} edges.")
            
            if self.on_cad_data:
                print(f"[ADA DEBUG] [SEND] Dispatching data to frontend callback...")
                self.on_cad_data(cad_data)
                print(f"[ADA DEBUG] [SENT] Dispatch complete.")
            
            # Save to Project
            if 'file_path' in cad_data:
                self.project_manager.save_cad_artifact(cad_data['file_path'], prompt)
            else:
                 # Fallback (legacy support)
                 self.project_manager.save_cad_artifact("output.stl", prompt)

            # Notify the model that the task is done - this triggers speech about completion
            completion_msg = "System Notification: CAD generation is complete! The 3D model is now displayed for the user. Let them know it's ready."
            try:
                await self.session.send(input=completion_msg, end_of_turn=True)
                print(f"[ADA DEBUG] [NOTE] Sent completion notification to model.")
            except Exception as e:
                 print(f"[ADA DEBUG] [ERR] Failed to send completion notification: {e}")

        else:
            print(f"[ADA DEBUG] [ERR] CadAgent returned None.")
            # Optionally notify failure
            try:
                await self.session.send(input="System Notification: CAD generation failed.", end_of_turn=True)
            except Exception:
                pass



    async def handle_write_file(self, path, content):
        print(f"[ADA DEBUG] [FS] Writing file: '{path}'")
        
        # Auto-create project if stuck in temp
        if self.project_manager.current_project == "temp":
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            new_project_name = f"Project_{timestamp}"
            print(f"[ADA DEBUG] [FS] Auto-creating project: {new_project_name}")
            
            success, msg = self.project_manager.create_project(new_project_name)
            if success:
                self.project_manager.switch_project(new_project_name)
                # Notify User
                try:
                    await self.session.send(input=f"System Notification: Automatic Project Creation. Switched to new project '{new_project_name}'.", end_of_turn=False)
                    if self.on_project_update:
                         self.on_project_update(new_project_name)
                except Exception as e:
                    print(f"[ADA DEBUG] [ERR] Failed to notify auto-project: {e}")
        
        # Force path to be relative to current project
        # If absolute path is provided, we try to strip it or just ignore it and use basename
        filename = os.path.basename(path)
        
        # If path contained subdirectories (e.g. "backend/server.py"), preserving that structure might be desired IF it's within the project.
        # But for safety, and per user request to "always create the file in the project", 
        # we will root it in the current project path.
        
        current_project_path = self.project_manager.get_current_project_path()
        final_path = current_project_path / filename # Simple flat structure for now, or allow relative?
        
        # If the user specifically wanted a subfolder, they might have provided "sub/file.txt".
        # Let's support relative paths if they don't start with /
        if not os.path.isabs(path):
             final_path = current_project_path / path
        
        print(f"[ADA DEBUG] [FS] Resolved path: '{final_path}'")

        try:
            # Ensure parent exists
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            with open(final_path, 'w', encoding='utf-8') as f:
                f.write(content)
            result = f"File '{final_path.name}' written successfully to project '{self.project_manager.current_project}'."
        except Exception as e:
            result = f"Failed to write file '{path}': {str(e)}"

        print(f"[ADA DEBUG] [FS] Result: {result}")
        try:
             await self.session.send(input=f"System Notification: {result}", end_of_turn=True)
        except Exception as e:
             print(f"[ADA DEBUG] [ERR] Failed to send fs result: {e}")

    async def handle_read_directory(self, path):
        print(f"[ADA DEBUG] [FS] Reading directory: '{path}'")
        try:
            if not os.path.exists(path):
                result = f"Directory '{path}' does not exist."
            else:
                items = os.listdir(path)
                result = f"Contents of '{path}': {', '.join(items)}"
        except Exception as e:
            result = f"Failed to read directory '{path}': {str(e)}"

        print(f"[ADA DEBUG] [FS] Result: {result}")
        try:
             await self.session.send(input=f"System Notification: {result}", end_of_turn=True)
        except Exception as e:
             print(f"[ADA DEBUG] [ERR] Failed to send fs result: {e}")

    async def handle_read_file(self, path):
        print(f"[ADA DEBUG] [FS] Reading file: '{path}'")
        try:
            if not os.path.exists(path):
                result = f"File '{path}' does not exist."
            else:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                result = f"Content of '{path}':\n{content}"
        except Exception as e:
            result = f"Failed to read file '{path}': {str(e)}"

        print(f"[ADA DEBUG] [FS] Result: {result}")
        try:
             await self.session.send(input=f"System Notification: {result}", end_of_turn=True)
        except Exception as e:
             print(f"[ADA DEBUG] [ERR] Failed to send fs result: {e}")

    async def handle_web_agent_request(self, prompt):
        print(f"[ADA DEBUG] [WEB] Web Agent Task: '{prompt}'")
        
        async def update_frontend(image_b64, log_text):
            if self.on_web_data:
                 self.on_web_data({"image": image_b64, "log": log_text})
                 
        # Run the web agent and wait for it to return
        result = await self.web_agent.run_task(prompt, update_callback=update_frontend)
        print(f"[ADA DEBUG] [WEB] Web Agent Task Returned: {result}")
        
        # Send the final result back to the main model
        try:
             await self.session.send(input=f"System Notification: Web Agent has finished.\nResult: {result}", end_of_turn=True)
        except Exception as e:
             print(f"[ADA DEBUG] [ERR] Failed to send web agent result to model: {e}")

    async def handle_portainer_call(self, tool: str, arguments: dict | None = None):
        if not tool:
            return

        try:
            if self._one_mcp is None:
                self._one_mcp = _get_one_mcp_client()

            if self._one_mcp is None:
                raise RuntimeError("ONE_MCP_URL is not set; cannot call Portainer tools")

            tool_name = tool
            # Convenience: allow passing just "listStacks" etc
            if not tool_name.startswith("portainer_1mcp_") and not tool_name.startswith("portainer_"):
                tool_name = f"portainer_1mcp_{tool_name}"
            result = await self._one_mcp.call_tool(tool_name, arguments or {})

            try:
                await self.session.send(
                    input=f"System Notification: Portainer MCP tool '{tool}' result:\n{result}",
                    end_of_turn=True,
                )
            except Exception as e:
                print(f"[ADA DEBUG] [ERR] Failed to send Portainer MCP result: {e}")
        except Exception as e:
            msg = (
                f"System Notification: Portainer tool call failed for '{tool}'.\n"
                f"Error: {e}\n\n"
                "Set ONE_MCP_URL for the backend container (1MCP required)."
            )
            try:
                await self.session.send(input=msg, end_of_turn=True)
            except Exception:
                pass

    async def handle_list_mcp_tools(self, prefix: str | None = None):
        try:
            if self._one_mcp is None:
                self._one_mcp = _get_one_mcp_client()
            if self._one_mcp is None:
                raise RuntimeError("ONE_MCP_URL is not set; cannot list MCP tools")

            data = await self._one_mcp.list_tools()
            tools = (data or {}).get("tools") if isinstance(data, dict) else None
            if tools is None:
                tools = data

            if isinstance(prefix, str) and prefix:
                tools = [t for t in (tools or []) if isinstance(t, dict) and str(t.get("name", "")).startswith(prefix)]

            # Keep payload small but useful: name + description + inputSchema
            normalized = []
            for t in (tools or []):
                if not isinstance(t, dict):
                    continue
                normalized.append(
                    {
                        "name": t.get("name"),
                        "description": t.get("description"),
                        "inputSchema": t.get("inputSchema") or t.get("input_schema"),
                    }
                )

            await self.session.send(
                input=f"System Notification: MCP tools list ({len(normalized)} tools).\n{normalized}",
                end_of_turn=True,
            )
        except Exception as e:
            try:
                await self.session.send(
                    input=f"System Notification: Failed to list MCP tools: {e}",
                    end_of_turn=True,
                )
            except Exception:
                pass

    async def _run_tool_background_then_send(self, label: str, coro):
        try:
            result = await coro
            msg = f"System Notification: {label} result:\n{result}"
        except Exception as e:
            msg = f"System Notification: {label} failed: {e}"

        try:
            # This triggers a follow-up model response.
            await self.session.send(input=msg, end_of_turn=True)
        except Exception as e:
            print(f"[ADA DEBUG] [TOOL] Background tool follow-up send failed: {e}")

    async def receive_audio(self):
        "Background task to reads from the websocket and write pcm chunks to the output queue"
        try:
            while True:
                turn = self.session.receive()
                async for response in turn:
                    # 1. Handle Audio Data
                    if data := response.data:
                        self.audio_in_queue.put_nowait(data)
                        # NOTE: 'continue' removed here to allow processing transcription/tools in same packet

                    # 2. Handle Transcription (User & Model)
                    if response.server_content:
                        if response.server_content.input_transcription:
                            transcript = response.server_content.input_transcription.text
                            if transcript:
                                # Skip if this is an exact duplicate event
                                if transcript != self._last_input_transcription:
                                    # Calculate delta (Gemini may send cumulative or chunk-based text)
                                    delta = transcript
                                    if transcript.startswith(self._last_input_transcription):
                                        delta = transcript[len(self._last_input_transcription):]
                                    self._last_input_transcription = transcript
                                    
                                    # Only send if there's new text
                                    if delta:
                                        # User is speaking, so interrupt model playback!
                                        self.clear_audio_queue()

                                        # Send to frontend (Streaming)
                                        if self.on_transcription:
                                             self.on_transcription({"sender": "User", "text": delta})
                                        
                                        # Buffer for Logging
                                        if self.chat_buffer["sender"] != "User":
                                            # Flush previous if exists
                                            if self.chat_buffer["sender"] and self.chat_buffer["text"].strip():
                                                self.project_manager.log_chat(self.chat_buffer["sender"], self.chat_buffer["text"])
                                            # Start new
                                            self.chat_buffer = {"sender": "User", "text": delta}
                                        else:
                                            # Append
                                            self.chat_buffer["text"] += delta
                        
                        if response.server_content.output_transcription:
                            transcript = response.server_content.output_transcription.text
                            if transcript:
                                # Skip if this is an exact duplicate event
                                if transcript != self._last_output_transcription:
                                    # Calculate delta (Gemini may send cumulative or chunk-based text)
                                    delta = transcript
                                    if transcript.startswith(self._last_output_transcription):
                                        delta = transcript[len(self._last_output_transcription):]
                                    self._last_output_transcription = transcript
                                    
                                    # Only send if there's new text
                                    if delta:
                                        # Send to frontend (Streaming)
                                        if self.on_transcription:
                                             self.on_transcription({"sender": "ADA", "text": delta})
                                        
                                        # Buffer for Logging
                                        if self.chat_buffer["sender"] != "ADA":
                                            # Flush previous
                                            if self.chat_buffer["sender"] and self.chat_buffer["text"].strip():
                                                self.project_manager.log_chat(self.chat_buffer["sender"], self.chat_buffer["text"])
                                            # Start new
                                            self.chat_buffer = {"sender": "ADA", "text": delta}
                                        else:
                                            # Append
                                            self.chat_buffer["text"] += delta
                        
                        # Flush buffer on turn completion if needed, 
                        # but usually better to wait for sender switch or explicit end.
                        # We can also check turn_complete signal if available in response.server_content.model_turn etc

                    # 3. Handle Tool Calls
                    if response.tool_call:
                        print("The tool was called")
                        function_responses = []
                        for fc in response.tool_call.function_calls:
                            if fc.name in ["generate_cad", "portainer_call", "list_mcp_tools", "run_web_agent", "write_file", "read_directory", "read_file", "create_project", "switch_project", "list_projects", "list_smart_devices", "control_light", "discover_printers", "print_stl", "get_print_status", "iterate_cad"]:
                                prompt = fc.args.get("prompt", "") # Prompt is not present for all tools
                                
                                # Check Permissions (Default to True if not set)
                                confirmation_required = self.permissions.get(fc.name, True)
                                
                                if not confirmation_required:
                                    print(f"[ADA DEBUG] [TOOL] Permission check: '{fc.name}' -> AUTO-ALLOW")
                                    # Skip confirmation block and jump to execution
                                    pass
                                else:
                                    # Confirmation Logic
                                    if not self.on_tool_confirmation:
                                        # No confirmation UI is wired up; default to allow.
                                        confirmed = True
                                    else:
                                        import uuid
                                        request_id = str(uuid.uuid4())
                                        print(f"[ADA DEBUG] [STOP] Requesting confirmation for '{fc.name}' (ID: {request_id})")

                                        future = asyncio.Future()
                                        self._pending_confirmations[request_id] = future

                                        self.on_tool_confirmation({
                                            "id": request_id,
                                            "tool": fc.name,
                                            "args": fc.args,
                                        })

                                        try:
                                            # Wait for user response
                                            confirmed = await future
                                        finally:
                                            self._pending_confirmations.pop(request_id, None)

                                        print(f"[ADA DEBUG] [CONFIRM] Request {request_id} resolved. Confirmed: {confirmed}")

                                    if not confirmed:
                                        print(f"[ADA DEBUG] [DENY] Tool call '{fc.name}' denied by user.")
                                        function_response = types.FunctionResponse(
                                            id=fc.id,
                                            name=fc.name,
                                            response={
                                                "result": "User denied the request to use this tool.",
                                            },
                                        )
                                        function_responses.append(function_response)
                                        continue

                                # If confirmed (or no callback configured, or auto-allowed), proceed
                                if fc.name == "generate_cad":
                                    print(f"\n[ADA DEBUG] --------------------------------------------------")
                                    print(f"[ADA DEBUG] [TOOL] Tool Call Detected: 'generate_cad'")
                                    print(f"[ADA DEBUG] [IN] Arguments: prompt='{prompt}'")
                                    
                                    asyncio.create_task(self.handle_cad_request(prompt))
                                    # No function response needed - model already acknowledged when user asked
                                
                                elif fc.name == "portainer_call":
                                    tool = fc.args.get("tool")
                                    arguments = fc.args.get("arguments") or {}
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'portainer_call' tool='{tool}'")
                                    if _env_true("ADA_ASYNC_TOOL_CALLS", False):
                                        # Fast ack: let the model respond immediately; follow-up is sent async.
                                        function_responses.append(
                                            types.FunctionResponse(
                                                id=fc.id,
                                                name=fc.name,
                                                response={"result": f"Starting portainer_call '{tool}'. Results will follow."},
                                            )
                                        )

                                        timeout_s = float(os.getenv("ADA_TOOL_CALL_TIMEOUT_S") or 10.0)
                                        if self._one_mcp is None:
                                            self._one_mcp = _get_one_mcp_client()
                                        tool_name = tool or ""
                                        if tool_name and not tool_name.startswith("portainer_1mcp_") and not tool_name.startswith("portainer_"):
                                            tool_name = f"portainer_1mcp_{tool_name}"

                                        async def _coro():
                                            if not tool_name:
                                                raise RuntimeError("Missing tool")
                                            if self._one_mcp is None:
                                                raise RuntimeError("ONE_MCP_URL is not set; cannot call Portainer tools")
                                            return await asyncio.wait_for(
                                                self._one_mcp.call_tool(tool_name, arguments or {}),
                                                timeout=timeout_s,
                                            )

                                        asyncio.create_task(
                                            self._run_tool_background_then_send(
                                                f"Portainer '{tool}'",
                                                _coro(),
                                            )
                                        )
                                    else:
                                        result_text = "Portainer MCP tool call failed."
                                        try:
                                            timeout_s = float(os.getenv("ADA_TOOL_CALL_TIMEOUT_S") or 10.0)
                                            if not tool:
                                                raise RuntimeError("Missing tool")

                                            if self._one_mcp is None:
                                                self._one_mcp = _get_one_mcp_client()
                                            if self._one_mcp is None:
                                                raise RuntimeError("ONE_MCP_URL is not set; cannot call Portainer tools")

                                            tool_name = tool
                                            if not tool_name.startswith("portainer_1mcp_") and not tool_name.startswith("portainer_"):
                                                tool_name = f"portainer_1mcp_{tool_name}"

                                            result = await asyncio.wait_for(
                                                self._one_mcp.call_tool(tool_name, arguments or {}),
                                                timeout=timeout_s,
                                            )

                                            result_text = f"Portainer tool '{tool}' ok."
                                            try:
                                                self._emit_system_status(f"Portainer '{tool}' result: {result}")
                                            except Exception:
                                                pass
                                        except asyncio.TimeoutError:
                                            result_text = "Portainer tool call timed out."
                                        except Exception as e:
                                            result_text = f"Portainer tool call failed: {e}"

                                        function_responses.append(
                                            types.FunctionResponse(
                                                id=fc.id,
                                                name=fc.name,
                                                response={"result": result_text},
                                            )
                                        )

                                elif fc.name == "list_mcp_tools":
                                    prefix = fc.args.get("prefix")
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'list_mcp_tools' prefix='{prefix}'")
                                    if _env_true("ADA_ASYNC_TOOL_CALLS", False):
                                        function_responses.append(
                                            types.FunctionResponse(
                                                id=fc.id,
                                                name=fc.name,
                                                response={"result": "Listing MCP tools now. Results will follow."},
                                            )
                                        )

                                        timeout_s = float(os.getenv("ADA_TOOL_CALL_TIMEOUT_S") or 10.0)

                                        async def _coro():
                                            if self._one_mcp is None:
                                                self._one_mcp = _get_one_mcp_client()
                                            if self._one_mcp is None:
                                                raise RuntimeError("ONE_MCP_URL is not set; cannot list MCP tools")

                                            data = await asyncio.wait_for(self._one_mcp.list_tools(), timeout=timeout_s)
                                            tools = (data or {}).get("tools") if isinstance(data, dict) else None
                                            if tools is None:
                                                tools = data
                                            if isinstance(prefix, str) and prefix:
                                                tools = [t for t in (tools or []) if isinstance(t, dict) and str(t.get("name", "")).startswith(prefix)]

                                            normalized = []
                                            for t in (tools or []):
                                                if not isinstance(t, dict):
                                                    continue
                                                normalized.append(
                                                    {
                                                        "name": t.get("name"),
                                                        "description": t.get("description"),
                                                        "inputSchema": t.get("inputSchema") or t.get("input_schema"),
                                                    }
                                                )
                                            total = len(normalized)
                                            preview_n = min(total, 30)
                                            preview = normalized[:preview_n]
                                            try:
                                                self._emit_system_status(f"MCP tools list ({total}): {preview}")
                                            except Exception:
                                                pass
                                            return f"Found {total} MCP tools" + (f" (prefix='{prefix}')" if isinstance(prefix, str) and prefix else "") + f". Preview: {preview}"

                                        asyncio.create_task(self._run_tool_background_then_send("MCP tools", _coro()))
                                    else:
                                        result_text = "MCP tools listing failed."
                                        try:
                                            timeout_s = float(os.getenv("ADA_TOOL_CALL_TIMEOUT_S") or 10.0)

                                            if self._one_mcp is None:
                                                self._one_mcp = _get_one_mcp_client()
                                            if self._one_mcp is None:
                                                raise RuntimeError("ONE_MCP_URL is not set; cannot list MCP tools")

                                            data = await asyncio.wait_for(self._one_mcp.list_tools(), timeout=timeout_s)
                                            tools = (data or {}).get("tools") if isinstance(data, dict) else None
                                            if tools is None:
                                                tools = data

                                            if isinstance(prefix, str) and prefix:
                                                tools = [t for t in (tools or []) if isinstance(t, dict) and str(t.get("name", "")).startswith(prefix)]

                                            normalized = []
                                            for t in (tools or []):
                                                if not isinstance(t, dict):
                                                    continue
                                                normalized.append(
                                                    {
                                                        "name": t.get("name"),
                                                        "description": t.get("description"),
                                                        "inputSchema": t.get("inputSchema") or t.get("input_schema"),
                                                    }
                                                )

                                            total = len(normalized)
                                            preview_n = min(total, 30)
                                            preview = normalized[:preview_n]
                                            result_text = f"Found {total} MCP tools" + (f" (prefix='{prefix}')" if isinstance(prefix, str) and prefix else "") + f". Preview: {preview}"

                                            try:
                                                self._emit_system_status(f"MCP tools list ({total}): {preview}")
                                            except Exception:
                                                pass
                                        except asyncio.TimeoutError:
                                            result_text = "MCP tools listing timed out."
                                        except Exception as e:
                                            result_text = f"Failed to list MCP tools: {e}"

                                        function_responses.append(
                                            types.FunctionResponse(
                                                id=fc.id,
                                                name=fc.name,
                                                response={"result": result_text},
                                            )
                                        )

                                elif fc.name == "run_web_agent":
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'run_web_agent' with prompt='{prompt}'")
                                    asyncio.create_task(self.handle_web_agent_request(prompt))
                                    
                                    result_text = "Web Navigation started. Do not reply to this message."
                                    function_response = types.FunctionResponse(
                                        id=fc.id,
                                        name=fc.name,
                                        response={
                                            "result": result_text,
                                        }
                                    )
                                    print(f"[ADA DEBUG] [RESPONSE] Sending function response: {function_response}")
                                    function_responses.append(function_response)



                                elif fc.name == "write_file":
                                    path = fc.args["path"]
                                    content = fc.args["content"]
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'write_file' path='{path}'")
                                    asyncio.create_task(self.handle_write_file(path, content))
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": "Writing file..."}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "read_directory":
                                    path = fc.args["path"]
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'read_directory' path='{path}'")
                                    asyncio.create_task(self.handle_read_directory(path))
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": "Reading directory..."}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "read_file":
                                    path = fc.args["path"]
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'read_file' path='{path}'")
                                    asyncio.create_task(self.handle_read_file(path))
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": "Reading file..."}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "create_project":
                                    name = fc.args["name"]
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'create_project' name='{name}'")
                                    success, msg = self.project_manager.create_project(name)
                                    if success:
                                        # Auto-switch to the newly created project
                                        self.project_manager.switch_project(name)
                                        msg += f" Switched to '{name}'."
                                        if self.on_project_update:
                                            self.on_project_update(name)
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": msg}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "switch_project":
                                    name = fc.args["name"]
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'switch_project' name='{name}'")
                                    success, msg = self.project_manager.switch_project(name)
                                    if success:
                                        if self.on_project_update:
                                            self.on_project_update(name)
                                        # Gather project context and send to AI (silently, no response expected)
                                        context = self.project_manager.get_project_context()
                                        print(f"[ADA DEBUG] [PROJECT] Sending project context to AI ({len(context)} chars)")
                                        try:
                                            await self.session.send(input=f"System Notification: {msg}\n\n{context}", end_of_turn=False)
                                        except Exception as e:
                                            print(f"[ADA DEBUG] [ERR] Failed to send project context: {e}")
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": msg}
                                    )
                                    function_responses.append(function_response)
                                
                                elif fc.name == "list_projects":
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'list_projects'")
                                    projects = self.project_manager.list_projects()
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": f"Available projects: {', '.join(projects)}"}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "list_smart_devices":
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'list_smart_devices'")
                                    # Use cached devices directly for speed
                                    # devices_dict is {ip: SmartDevice}
                                    
                                    dev_summaries = []
                                    frontend_list = []
                                    
                                    for ip, d in self.kasa_agent.devices.items():
                                        dev_type = "unknown"
                                        if d.is_bulb: dev_type = "bulb"
                                        elif d.is_plug: dev_type = "plug"
                                        elif d.is_strip: dev_type = "strip"
                                        elif d.is_dimmer: dev_type = "dimmer"
                                        
                                        # Format for Model
                                        info = f"{d.alias} (IP: {ip}, Type: {dev_type})"
                                        if d.is_on:
                                            info += " [ON]"
                                        else:
                                            info += " [OFF]"
                                        dev_summaries.append(info)
                                        
                                        # Format for Frontend
                                        frontend_list.append({
                                            "ip": ip,
                                            "alias": d.alias,
                                            "model": d.model,
                                            "type": dev_type,
                                            "is_on": d.is_on,
                                            "brightness": d.brightness if d.is_bulb or d.is_dimmer else None,
                                            "hsv": d.hsv if d.is_bulb and d.is_color else None,
                                            "has_color": d.is_color if d.is_bulb else False,
                                            "has_brightness": d.is_dimmable if d.is_bulb or d.is_dimmer else False
                                        })
                                    
                                    result_str = "No devices found in cache."
                                    if dev_summaries:
                                        result_str = "Found Devices (Cached):\n" + "\n".join(dev_summaries)
                                    
                                    # Trigger frontend update
                                    if self.on_device_update:
                                        self.on_device_update(frontend_list)

                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "control_light":
                                    target = fc.args["target"]
                                    action = fc.args["action"]
                                    brightness = fc.args.get("brightness")
                                    color = fc.args.get("color")
                                    
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'control_light' Target='{target}' Action='{action}'")
                                    
                                    result_msg = f"Action '{action}' on '{target}' failed."
                                    success = False
                                    
                                    if action == "turn_on":
                                        success = await self.kasa_agent.turn_on(target)
                                        if success:
                                            result_msg = f"Turned ON '{target}'."
                                    elif action == "turn_off":
                                        success = await self.kasa_agent.turn_off(target)
                                        if success:
                                            result_msg = f"Turned OFF '{target}'."
                                    elif action == "set":
                                        success = True
                                        result_msg = f"Updated '{target}':"
                                    
                                    # Apply extra attributes if 'set' or if we just turned it on and want to set them too
                                    if success or action == "set":
                                        if brightness is not None:
                                            sb = await self.kasa_agent.set_brightness(target, brightness)
                                            if sb:
                                                result_msg += f" Set brightness to {brightness}."
                                        if color is not None:
                                            sc = await self.kasa_agent.set_color(target, color)
                                            if sc:
                                                result_msg += f" Set color to {color}."

                                    # Notify Frontend of State Change
                                    if success:
                                        # We don't need full discovery, just refresh known state or push update
                                        # But for simplicity, let's get the standard list representation
                                        # KasaAgent updates its internal state on control, so we can rebuild the list
                                        
                                        # Quick rebuild of list from internal dict
                                        updated_list = []
                                        for ip, dev in self.kasa_agent.devices.items():
                                            # We need to ensure we have the correct dict structure expected by frontend
                                            # We duplicate logic from KasaAgent.discover_devices a bit, but that's okay for now or we can add a helper
                                            # Ideally KasaAgent has a 'get_devices_list()' method.
                                            # Use the cached objects in self.kasa_agent.devices
                                            
                                            dev_type = "unknown"
                                            if dev.is_bulb: dev_type = "bulb"
                                            elif dev.is_plug: dev_type = "plug"
                                            elif dev.is_strip: dev_type = "strip"
                                            elif dev.is_dimmer: dev_type = "dimmer"

                                            d_info = {
                                                "ip": ip,
                                                "alias": dev.alias,
                                                "model": dev.model,
                                                "type": dev_type,
                                                "is_on": dev.is_on,
                                                "brightness": dev.brightness if dev.is_bulb or dev.is_dimmer else None,
                                                "hsv": dev.hsv if dev.is_bulb and dev.is_color else None,
                                                "has_color": dev.is_color if dev.is_bulb else False,
                                                "has_brightness": dev.is_dimmable if dev.is_bulb or dev.is_dimmer else False
                                            }
                                            updated_list.append(d_info)
                                            
                                        if self.on_device_update:
                                            self.on_device_update(updated_list)
                                    else:
                                        # Report Error
                                        if self.on_error:
                                            self.on_error(result_msg)

                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_msg}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "discover_printers":
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'discover_printers'")
                                    printers = await self.printer_agent.discover_printers()
                                    # Format for model
                                    if printers:
                                        printer_list = []
                                        for p in printers:
                                            printer_list.append(f"{p['name']} ({p['host']}:{p['port']}, type: {p['printer_type']})")
                                        result_str = "Found Printers:\n" + "\n".join(printer_list)
                                    else:
                                        result_str = "No printers found on network. Ensure printers are on and running OctoPrint/Moonraker."
                                    
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "print_stl":
                                    stl_path = fc.args["stl_path"]
                                    printer = fc.args["printer"]
                                    profile = fc.args.get("profile")
                                    
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'print_stl' STL='{stl_path}' Printer='{printer}'")
                                    
                                    # Resolve 'current' to project STL
                                    if stl_path.lower() == "current":
                                        stl_path = "output.stl" # Let printer agent resolve it in root_path

                                    # Get current project path
                                    project_path = str(self.project_manager.get_current_project_path())
                                    
                                    result = await self.printer_agent.print_stl(
                                        stl_path, 
                                        printer, 
                                        profile, 
                                        root_path=project_path
                                    )
                                    result_str = result.get("message", "Unknown result")
                                    
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "get_print_status":
                                    printer = fc.args["printer"]
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'get_print_status' Printer='{printer}'")
                                    
                                    status = await self.printer_agent.get_print_status(printer)
                                    if status:
                                        result_str = f"Printer: {status.printer}\n"
                                        result_str += f"State: {status.state}\n"
                                        result_str += f"Progress: {status.progress_percent:.1f}%\n"
                                        if status.time_remaining:
                                            result_str += f"Time Remaining: {status.time_remaining}\n"
                                        if status.time_elapsed:
                                            result_str += f"Time Elapsed: {status.time_elapsed}\n"
                                        if status.filename:
                                            result_str += f"File: {status.filename}\n"
                                        if status.temperatures:
                                            temps = status.temperatures
                                            if "hotend" in temps:
                                                result_str += f"Hotend: {temps['hotend']['current']:.0f}°C / {temps['hotend']['target']:.0f}°C\n"
                                            if "bed" in temps:
                                                result_str += f"Bed: {temps['bed']['current']:.0f}°C / {temps['bed']['target']:.0f}°C"
                                    else:
                                        result_str = f"Could not get status for printer '{printer}'. Ensure it is discovered first."
                                    
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)

                                elif fc.name == "iterate_cad":
                                    prompt = fc.args["prompt"]
                                    print(f"[ADA DEBUG] [TOOL] Tool Call: 'iterate_cad' Prompt='{prompt}'")
                                    
                                    # Emit status
                                    if self.on_cad_status:
                                        self.on_cad_status("generating")
                                    
                                    # Get project cad folder path
                                    cad_output_dir = str(self.project_manager.get_current_project_path() / "cad")
                                    
                                    # Call CadAgent to iterate on the design
                                    cad_data = await self.cad_agent.iterate_prototype(prompt, output_dir=cad_output_dir)
                                    
                                    if cad_data:
                                        print(f"[ADA DEBUG] [OK] CadAgent iteration returned data successfully.")
                                        
                                        # Dispatch to frontend
                                        if self.on_cad_data:
                                            print(f"[ADA DEBUG] [SEND] Dispatching iterated CAD data to frontend...")
                                            self.on_cad_data(cad_data)
                                            print(f"[ADA DEBUG] [SENT] Dispatch complete.")
                                        
                                        # Save to Project
                                        self.project_manager.save_cad_artifact("output.stl", f"Iteration: {prompt}")
                                        
                                        result_str = f"Successfully iterated design: {prompt}. The updated 3D model is now displayed."
                                    else:
                                        print(f"[ADA DEBUG] [ERR] CadAgent iteration returned None.")
                                        result_str = f"Failed to iterate design with prompt: {prompt}"
                                    
                                    function_response = types.FunctionResponse(
                                        id=fc.id, name=fc.name, response={"result": result_str}
                                    )
                                    function_responses.append(function_response)
                        if function_responses:
                            # IMPORTANT:
                            # Some Gemini Live deployments/models do not support native tool responses
                            # (send_tool_response / ToolResponse). Attempting them can close the websocket
                            # with 1008 policy violations.
                            # Default to NOT sending native tool responses; optionally send results as text.
                            await self._send_tool_results(function_responses)
                
                # Turn/Response Loop Finished
                self.flush_chat()

                while not self.audio_in_queue.empty():
                    self.audio_in_queue.get_nowait()
        except ConnectionClosedError as e:
            try:
                code = getattr(e, "code", None)
                reason = getattr(e, "reason", None)
                self._emit_system_status(
                    f"Gemini Live disconnected (code={code}, reason={reason}). "
                    "This usually means the configured MODEL isn't available for your API key/region, or the request was rejected."
                )
            except Exception:
                pass
            print(f"[ADA DEBUG] [LIVE] Websocket closed: {e}")
            raise
        except Exception as e:
            print(f"Error in receive_audio: {e}")
            traceback.print_exc()
            raise

    async def _send_tool_results(self, function_responses):
        # Native tool responses are opt-in, because they can trigger 1008 policy violations
        # on some Live endpoints.
        if _env_true("ADA_USE_NATIVE_TOOL_RESPONSES", False):
            try:
                send_tool_response = getattr(self.session, "send_tool_response", None)
                if callable(send_tool_response):
                    await send_tool_response(function_responses=function_responses)
                    return
            except Exception as e:
                print(f"[ADA DEBUG] [TOOL] send_tool_response failed: {e}")

            try:
                tool_response_cls = getattr(types, "ToolResponse", None)
                if tool_response_cls is not None:
                    await self.session.send(input=tool_response_cls(function_responses=function_responses), end_of_turn=True)
                    return
            except Exception as e:
                print(f"[ADA DEBUG] [TOOL] ToolResponse fallback failed: {e}")

        # Default: send tool results as plain text (safe across endpoints).
        if not _env_true("ADA_TOOL_RESPONSE_TEXT_FALLBACK", True):
            return

        try:
            lines = []
            for fr in function_responses:
                try:
                    name = getattr(fr, "name", None)
                    payload = getattr(fr, "response", None)
                    lines.append(f"{name}: {payload}")
                except Exception:
                    lines.append(str(fr))
            msg = "System Notification: Tool results:\n" + "\n".join(lines)
            await self.session.send(input=msg, end_of_turn=True)
        except Exception as e:
            print(f"[ADA DEBUG] [TOOL] Plain text tool fallback failed: {e}")

    async def play_audio(self):
        if self.use_browser_audio:
            while True:
                bytestream = await self.audio_in_queue.get()
                if self.on_audio_data:
                    self.on_audio_data(bytestream)
            return
        pya = _get_pyaudio()
        stream = await asyncio.to_thread(
            pya.open,
            format=_get_pyaudio_mod().paInt16,
            channels=CHANNELS,
            rate=RECEIVE_SAMPLE_RATE,
            output=True,
            output_device_index=self.output_device_index,
        )
        while True:
            bytestream = await self.audio_in_queue.get()
            if self.on_audio_data:
                self.on_audio_data(bytestream)
            await asyncio.to_thread(stream.write, bytestream)

    async def get_frames(self):
        cap = await asyncio.to_thread(cv2.VideoCapture, 0, cv2.CAP_AVFOUNDATION)
        while True:
            if self.paused:
                await asyncio.sleep(0.1)
                continue
            frame = await asyncio.to_thread(self._get_frame, cap)
            if frame is None:
                break
            await asyncio.sleep(1.0)
            if self.out_queue:
                await self.out_queue.put(frame)
        cap.release()

    def _get_frame(self, cap):
        ret, frame = cap.read()
        if not ret:
            return None
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = PIL.Image.fromarray(frame_rgb)
        img.thumbnail([1024, 1024])
        image_io = io.BytesIO()
        img.save(image_io, format="jpeg")
        image_io.seek(0)
        image_bytes = image_io.read()
        return {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode()}

    async def _get_screen(self):
        pass 
    async def get_screen(self):
         pass

    async def run(self, start_message=None):
        retry_delay = 1
        is_reconnect = False
        
        while not self.stop_event.is_set():
            try:
                print(f"[ADA DEBUG] [CONNECT] Connecting to Gemini Live API...")

                try:
                    decls = tools[1].get("function_declarations", []) if isinstance(tools, list) and len(tools) > 1 else []
                    names = []
                    for d in decls:
                        if isinstance(d, dict):
                            pname = d.get("name")
                            ptype = None
                            params = d.get("parameters")
                            if isinstance(params, dict):
                                ptype = params.get("type")
                            names.append(f"{pname}({ptype})")
                    print(f"[ADA DEBUG] [TOOLS] Registered tools: {names}")
                    print(f"[ADA DEBUG] [TOOLS] ADA_DISABLE_AUDIO_SEND={_env_true('ADA_DISABLE_AUDIO_SEND', False)} ADA_DISABLE_IMAGE_SEND={_env_true('ADA_DISABLE_IMAGE_SEND', False)}")
                except Exception as e:
                    print(f"[ADA DEBUG] [ERR] Failed to log tool registration: {e}")

                async with (
                    _get_genai_client().aio.live.connect(model=MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session = session

                    try:
                        await self._init_tools_on_connect()
                    except Exception:
                        pass

                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue = asyncio.Queue(maxsize=10)

                    tg.create_task(self.send_realtime())
                    if not self.use_browser_audio:
                        tg.create_task(self.listen_audio())
                    # tg.create_task(self._process_video_queue()) # Removed in favor of VAD

                    if self.video_mode == "camera":
                        tg.create_task(self.get_frames())
                    elif self.video_mode == "screen":
                        tg.create_task(self.get_screen())

                    tg.create_task(self.receive_audio())
                    tg.create_task(self.play_audio())

                    # Handle Startup vs Reconnect Logic
                    if not is_reconnect:
                        if start_message:
                            print(f"[ADA DEBUG] [INFO] Sending start message: {start_message}")
                            await self.session.send(input=start_message, end_of_turn=True)
                        
                        # Sync Project State
                        if self.on_project_update and self.project_manager:
                            self.on_project_update(self.project_manager.current_project)
                    
                    else:
                        print(f"[ADA DEBUG] [RECONNECT] Connection restored.")
                        # Restore Context
                        print(f"[ADA DEBUG] [RECONNECT] Fetching recent chat history to restore context...")
                        history = self.project_manager.get_recent_chat_history(limit=10)
                        
                        context_msg = "System Notification: Connection was lost and just re-established. Here is the recent chat history to help you resume seamlessly:\n\n"
                        for entry in history:
                            sender = entry.get('sender', 'Unknown')
                            text = entry.get('text', '')
                            context_msg += f"[{sender}]: {text}\n"
                        
                        context_msg += "\nPlease acknowledge the reconnection to the user (e.g. 'I lost connection for a moment, but I'm back...') and resume what you were doing."
                        
                        print(f"[ADA DEBUG] [RECONNECT] Sending restoration context to model...")
                        await self.session.send(input=context_msg, end_of_turn=True)

                    # Reset retry delay on successful connection
                    retry_delay = 1
                    
                    # Wait until stop event, or until the session task group exits (which happens on error)
                    # Actually, the TaskGroup context manager will exit if any tasks fail/cancel.
                    # We need to keep this block alive.
                    # The original code just waited on stop_event, but that doesn't account for session death.
                    # We should rely on the TaskGroup raising an exception when subtasks fail (like receive_audio).
                    
                    # However, since receive_audio is a task in the group, if it crashes (connection closed), 
                    # the group will cancel others and exit. We catch that exit below.
                    
                    # We can await stop_event, but if the connection dies, receive_audio crashes -> group closes -> we exit `async with` -> restart loop.
                    # To ensure we don't block indefinitely if connection dies silently (unlikely with receive_audio), we just wait.
                    await self.stop_event.wait()

            except asyncio.CancelledError:
                print(f"[ADA DEBUG] [STOP] Main loop cancelled.")
                break
                
            except Exception as e:
                # This catches the ExceptionGroup from TaskGroup or direct exceptions
                print(f"[ADA DEBUG] [ERR] Connection Error: {e}")
                try:
                    if isinstance(e, BaseExceptionGroup):
                        for i, sub in enumerate(e.exceptions):
                            print(f"[ADA DEBUG] [ERR]  sub[{i}]: {sub}")
                except Exception:
                    pass
                
                if self.stop_event.is_set():
                    break

                # When the upstream service is unavailable or timing out, be gentle.
                # Short, repeated reconnects can worsen the situation.
                emsg = str(e).lower()
                if "service is currently unavailable" in emsg or "deadline expired" in emsg:
                    retry_delay = max(retry_delay, 5)

                # Add a small jitter to avoid synchronized reconnect storms.
                jitter = random.uniform(0.0, min(1.0, retry_delay * 0.1))
                delay = retry_delay + jitter
                print(f"[ADA DEBUG] [RETRY] Reconnecting in {delay:.2f} seconds...")
                await asyncio.sleep(delay)
                retry_delay = min(retry_delay * 2, 60) # Exponential backoff capped at 60s
                is_reconnect = True # Next loop will be a reconnect
                
            finally:
                # Cleanup before retry
                if hasattr(self, 'audio_stream') and self.audio_stream:
                    try:
                        self.audio_stream.close()
                    except: 
                        pass

def get_input_devices():
    import pyaudio
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    numdevices = info.get('deviceCount')
    devices = []
    for i in range(0, numdevices):
        if (p.get_device_info_by_host_api_device_index(0, i).get('maxInputChannels')) > 0:
            devices.append((i, p.get_device_info_by_host_api_device_index(0, i).get('name')))
    p.terminate()
    return devices

def get_output_devices():
    import pyaudio
    p = pyaudio.PyAudio()
    info = p.get_host_api_info_by_index(0)
    numdevices = info.get('deviceCount')
    devices = []
    for i in range(0, numdevices):
        if (p.get_device_info_by_host_api_device_index(0, i).get('maxOutputChannels')) > 0:
            devices.append((i, p.get_device_info_by_host_api_device_index(0, i).get('name')))
    p.terminate()
    return devices

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        type=str,
        default=DEFAULT_MODE,
        help="pixels to stream from",
        choices=["camera", "screen", "none"],
    )
    args = parser.parse_args()
    main = AudioLoop(video_mode=args.mode)
    asyncio.run(main.run())