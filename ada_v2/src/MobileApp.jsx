import React, { useEffect, useMemo, useRef, useState } from 'react';
import io from 'socket.io-client';

function MobileApp() {
    const backendUrl = useMemo(() => {
        const envUrl = import.meta?.env?.VITE_BACKEND_URL;
        if (envUrl) return envUrl;
        const host = typeof window !== 'undefined' ? window.location.hostname : 'localhost';
        const isLocalhost = host === 'localhost' || host === '127.0.0.1' || host === '::1';
        if (isLocalhost) return 'http://localhost:8000';
        return typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000';
    }, []);

    const socket = useMemo(() => io(backendUrl), [backendUrl]);

    const [socketConnected, setSocketConnected] = useState(socket.connected);
    const [messages, setMessages] = useState([]); // [{role, text}]
    const [inputValue, setInputValue] = useState('');
    const [ttsEnabled, setTtsEnabled] = useState(true);

    const listEndRef = useRef(null);

    const stopSpeaking = () => {
        try {
            if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
                window.speechSynthesis.cancel();
            }
        } catch (e) {
            // ignore
        }
    };

    const speak = (text) => {
        try {
            if (!ttsEnabled) return;
            if (!text) return;
            if (typeof window === 'undefined' || !('speechSynthesis' in window)) return;
            window.speechSynthesis.cancel();
            const utter = new SpeechSynthesisUtterance(text);
            window.speechSynthesis.speak(utter);
        } catch (e) {
            // ignore
        }
    };

    const addMessage = (role, text) => {
        setMessages((prev) => [...prev, { role, text }]);
    };

    const send = () => {
        const text = inputValue.trim();
        if (!text) return;

        addMessage('You', text);
        socket.emit('chat_text', { text });
        setInputValue('');
    };

    useEffect(() => {
        const onConnect = () => setSocketConnected(true);
        const onDisconnect = () => setSocketConnected(false);

        socket.on('connect', onConnect);
        socket.on('disconnect', onDisconnect);

        socket.on('assistant_text', (payload) => {
            const text = typeof payload === 'string' ? payload : payload?.text;
            if (!text) return;
            addMessage('ADA', text);
            speak(text);
        });

        return () => {
            socket.off('connect', onConnect);
            socket.off('disconnect', onDisconnect);
            socket.off('assistant_text');
            socket.disconnect();
        };
    }, [socket]);

    useEffect(() => {
        if (listEndRef.current) {
            listEndRef.current.scrollIntoView({ behavior: 'smooth' });
        }
    }, [messages]);

    return (
        <div className="w-screen h-screen bg-black text-green-100 flex flex-col">
            <div className="px-4 py-3 border-b border-white/10 bg-white/5 flex items-center justify-between">
                <div className="text-xs font-bold tracking-widest uppercase">ADA Mobile</div>
                <div className={`text-[10px] font-bold ${socketConnected ? 'text-green-400' : 'text-red-400'}`}>
                    {socketConnected ? 'Connected' : 'Disconnected'}
                </div>
            </div>

            <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
                {messages.length === 0 ? (
                    <div className="text-white/40 text-sm leading-relaxed">
                        Type a message. On iPhone you can also use the keyboard mic dictation.
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
                <div className="flex flex-col gap-2 sm:flex-row">
                    <button
                        className={`flex-1 py-2 rounded-xl text-sm font-bold border ${ttsEnabled ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-200' : 'bg-white/5 border-white/10 text-white/60'}`}
                        onClick={() => setTtsEnabled((v) => !v)}
                    >
                        {ttsEnabled ? 'TTS: ON' : 'TTS: OFF'}
                    </button>
                    <button
                        className="flex-1 py-2 rounded-xl text-sm font-bold border bg-red-500/10 border-red-500/30 text-red-200"
                        onClick={stopSpeaking}
                    >
                        Stop Speaking
                    </button>
                </div>

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
                        className="px-4 py-2 rounded-xl text-sm font-bold bg-green-500/20 hover:bg-green-500/30 border border-green-500/30 text-green-200"
                        onClick={send}
                    >
                        Send
                    </button>
                </div>
            </div>
        </div>
    );
}

export default MobileApp;
