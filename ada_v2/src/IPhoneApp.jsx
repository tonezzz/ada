import React, { useEffect, useMemo, useRef, useState } from 'react';
import io from 'socket.io-client';

function IPhoneApp() {
    const socket = useMemo(() => {
        const envUrl = import.meta?.env?.VITE_BACKEND_URL;
        if (envUrl) return io(envUrl, { transports: ['websocket'], upgrade: false });

        const hostname = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
        const protocol = typeof window !== 'undefined' ? window.location.protocol : 'http:';
        const port = typeof window !== 'undefined' ? window.location.port : '';
        const isLocalhost = hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '::1';
        if (isLocalhost) return io('http://localhost:8000', { transports: ['websocket'], upgrade: false });

        // If the UI is served by Vite on :5173, the backend is on :8000.
        if (port === '5173') return io(`${protocol}//${hostname}:8000`, { transports: ['websocket'], upgrade: false });

        return io(window.location.origin, { transports: ['websocket'], upgrade: false });
    }, []);

    const [socketConnected, setSocketConnected] = useState(socket.connected);
    const [modelConnected, setModelConnected] = useState(false);
    const [status, setStatus] = useState('Disconnected');
    const [messages, setMessages] = useState([]); // [{role, text}]
    const [inputValue, setInputValue] = useState('');

    // Streamed assistant audio playback
    const playbackAudioContextRef = useRef(null);
    const playbackGainRef = useRef(null);
    const playbackProcessorRef = useRef(null);
    const playbackQueueRef = useRef([]);
    const playbackQueueOffsetRef = useRef(0);
    const playbackBufferedSamplesRef = useRef(0);
    const playbackStartedRef = useRef(false);
    const hasStreamedAssistantAudioRef = useRef(false);
    const assistantAudioSrcRateRef = useRef(null);

    const listEndRef = useRef(null);

    const addMessage = (role, text) => {
        setMessages((prev) => [...prev, { role, text }]);
    };

    const stopAssistantPlayback = () => {
        try {
            if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
                window.speechSynthesis.cancel();
            }
        } catch (e) {
            // ignore
        }

        try {
            playbackQueueRef.current = [];
            playbackQueueOffsetRef.current = 0;
            playbackBufferedSamplesRef.current = 0;
            playbackStartedRef.current = false;
        } catch (e) {
            // ignore
        }
    };

    const send = () => {
        const text = inputValue.trim();
        if (!text) return;
        addMessage('You', text);
        socket.emit('chat_text', { text });
        setInputValue('');
    };

    const togglePower = () => {
        try {
            if (!socketConnected) return;
            if (modelConnected) {
                socket.emit('stop_audio');
                return;
            }

            // Start the backend session without requiring getUserMedia on iPhone.
            // This enables typed chat + streamed assistant audio.
            socket.emit('start_audio', { use_browser_audio: true, muted: true });
        } catch (e) {
            // ignore
        }
    };

    useEffect(() => {
        const onConnect = () => {
            setSocketConnected(true);
            setStatus('Connected');
        };
        const onDisconnect = () => {
            setSocketConnected(false);
            setModelConnected(false);
            setStatus('Disconnected');
        };

        const onStatus = (payload) => {
            const msg = payload?.msg || '';
            if (msg) {
                setStatus(msg);
                addMessage('System', msg);
                if (msg === 'A.D.A Started') setModelConnected(true);
                if (msg === 'A.D.A Stopped') setModelConnected(false);
            }
        };

        const onAssistantText = (payload) => {
            const text = (payload?.text || '').trim();
            if (!text) return;
            addMessage('ADA', text);
        };

        const onAssistantAudioFormat = (fmt) => {
            try {
                const sr = parseInt(fmt?.sampleRate, 10);
                if (Number.isFinite(sr) && sr > 0) {
                    assistantAudioSrcRateRef.current = sr;
                }
            } catch (e) {
                // ignore
            }
        };

        const onAssistantAudioChunk = (data) => {
            try {
                if (!data) return;

                if (!hasStreamedAssistantAudioRef.current) {
                    hasStreamedAssistantAudioRef.current = true;
                    stopAssistantPlayback();
                }

                let ab = null;
                if (data instanceof ArrayBuffer) {
                    ab = data;
                } else if (ArrayBuffer.isView(data) && data.buffer) {
                    ab = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
                } else if (data?.buffer instanceof ArrayBuffer) {
                    ab = data.buffer;
                }

                if (!ab || ab.byteLength < 2) return;
                const int16 = new Int16Array(ab);

                if (!playbackAudioContextRef.current) {
                    playbackAudioContextRef.current = new (window.AudioContext || window.webkitAudioContext)();
                }
                const pctx = playbackAudioContextRef.current;

                if (pctx.state === 'suspended') {
                    pctx.resume().catch(() => {});
                }

                if (!playbackGainRef.current) {
                    const g = pctx.createGain();
                    g.gain.value = 0.75;
                    g.connect(pctx.destination);
                    playbackGainRef.current = g;
                }

                if (!playbackProcessorRef.current) {
                    const processor = pctx.createScriptProcessor(4096, 0, 1);
                    playbackProcessorRef.current = processor;

                    processor.onaudioprocess = (evt) => {
                        const out = evt.outputBuffer.getChannelData(0);
                        out.fill(0);

                        const minBuffer = Math.floor((pctx.sampleRate || 48000) * 0.12);
                        if (!playbackStartedRef.current) {
                            if ((playbackBufferedSamplesRef.current || 0) < minBuffer) return;
                            playbackStartedRef.current = true;
                        }

                        let written = 0;
                        while (written < out.length && playbackQueueRef.current.length > 0) {
                            const head = playbackQueueRef.current[0];
                            const offset = playbackQueueOffsetRef.current || 0;
                            const available = head.length - offset;
                            if (available <= 0) {
                                playbackQueueRef.current.shift();
                                playbackQueueOffsetRef.current = 0;
                                continue;
                            }
                            const toCopy = Math.min(available, out.length - written);
                            out.set(head.subarray(offset, offset + toCopy), written);
                            written += toCopy;
                            playbackQueueOffsetRef.current = offset + toCopy;
                            playbackBufferedSamplesRef.current = Math.max(0, (playbackBufferedSamplesRef.current || 0) - toCopy);
                            if (playbackQueueOffsetRef.current >= head.length) {
                                playbackQueueRef.current.shift();
                                playbackQueueOffsetRef.current = 0;
                            }
                        }
                    };

                    processor.connect(playbackGainRef.current);
                }

                const float32 = new Float32Array(int16.length);
                for (let i = 0; i < int16.length; i++) {
                    float32[i] = int16[i] / 32768;
                }

                const srcRate = assistantAudioSrcRateRef.current || 24000;
                const dstRate = pctx.sampleRate || 48000;

                let out = float32;
                if (dstRate !== srcRate) {
                    const ratio = srcRate / dstRate;
                    const outLength = Math.floor(float32.length / ratio);
                    const resampled = new Float32Array(outLength);
                    for (let i = 0; i < outLength; i++) {
                        const pos = i * ratio;
                        const idx = Math.floor(pos);
                        const frac = pos - idx;
                        const a = float32[idx] ?? 0;
                        const b = float32[idx + 1] ?? a;
                        resampled[i] = a + (b - a) * frac;
                    }
                    out = resampled;
                }

                playbackQueueRef.current.push(out);
                playbackBufferedSamplesRef.current = (playbackBufferedSamplesRef.current || 0) + out.length;
            } catch (e) {
                // ignore
            }
        };

        const onAudioInterrupt = () => {
            stopAssistantPlayback();
        };

        socket.on('connect', onConnect);
        socket.on('disconnect', onDisconnect);
        socket.on('status', onStatus);
        socket.on('assistant_text', onAssistantText);
        socket.on('assistant_audio_format', onAssistantAudioFormat);
        socket.on('assistant_audio_chunk', onAssistantAudioChunk);
        socket.on('audio_interrupt', onAudioInterrupt);

        return () => {
            socket.off('connect', onConnect);
            socket.off('disconnect', onDisconnect);
            socket.off('status', onStatus);
            socket.off('assistant_text', onAssistantText);
            socket.off('assistant_audio_format', onAssistantAudioFormat);
            socket.off('assistant_audio_chunk', onAssistantAudioChunk);
            socket.off('audio_interrupt', onAudioInterrupt);
            socket.disconnect();

            try {
                if (playbackProcessorRef.current) {
                    playbackProcessorRef.current.disconnect();
                    playbackProcessorRef.current.onaudioprocess = null;
                }
            } catch (e) {
                // ignore
            }

            try {
                if (playbackAudioContextRef.current) {
                    playbackAudioContextRef.current.close();
                }
            } catch (e) {
                // ignore
            }
        };
    }, [socket]);

    useEffect(() => {
        if (listEndRef.current) {
            listEndRef.current.scrollIntoView({ behavior: 'smooth' });
        }
    }, [messages]);

    return (
        <div className="w-screen h-screen bg-black text-white flex flex-col">
            <div className="px-4 py-3 border-b border-white/10 bg-white/5 flex items-center justify-between">
                <div className="text-xs font-bold tracking-widest uppercase">ADA iPhone</div>
                <div className="flex items-center gap-2">
                    <button
                        className={`px-3 py-1 rounded-lg text-[11px] font-bold border ${modelConnected
                            ? 'bg-green-500/15 border-green-500/30 text-green-200'
                            : 'bg-white/5 border-white/10 text-white/70'
                            }`}
                        onClick={togglePower}
                        disabled={!socketConnected}
                    >
                        {modelConnected ? 'Power: ON' : 'Power: OFF'}
                    </button>
                    <div className={`text-[10px] font-bold ${socketConnected ? 'text-green-400' : 'text-red-400'}`}>
                        {socketConnected ? 'Socket: OK' : 'Socket: OFF'}
                    </div>
                </div>
            </div>

            <div className="px-4 py-2 text-[11px] text-white/60 border-b border-white/10 bg-black/40">
                {status}
            </div>

            <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
                {messages.length === 0 ? (
                    <div className="text-white/40 text-sm leading-relaxed">
                        Type a message to ADA.
                    </div>
                ) : null}

                {messages.map((m, idx) => (
                    <div key={idx} className={m.role === 'You' ? 'text-right' : 'text-left'}>
                        <div className="text-[10px] text-white/40 uppercase tracking-wider mb-1">{m.role}</div>
                        <div className={`inline-block max-w-[92%] rounded-2xl px-3 py-2 text-sm border ${
                            m.role === 'You'
                                ? 'bg-green-500/10 border-green-500/30 text-green-100'
                                : 'bg-white/5 border-white/10 text-white/90'
                        }`}>
                            {m.text}
                        </div>
                    </div>
                ))}
                <div ref={listEndRef} />
            </div>

            <div className="border-t border-white/10 bg-black/80 px-4 py-3 space-y-2">
                <div className="flex gap-2 items-center">
                    <input
                        value={inputValue}
                        onChange={(e) => setInputValue(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter') send();
                        }}
                        placeholder="Message ADA"
                        className="flex-1 bg-black/50 border border-white/10 rounded-xl px-3 py-2 text-sm outline-none focus:border-green-500/40 placeholder:text-white/30"
                    />
                    <button
                        className="px-4 py-2 rounded-xl text-sm font-bold bg-green-500/20 hover:bg-green-500/30 border border-green-500/30 text-green-200 disabled:opacity-40"
                        onClick={send}
                        disabled={!socketConnected}
                    >
                        Send
                    </button>
                </div>
            </div>
        </div>
    );
}

export default IPhoneApp;
