/**
 * Codiey — Browser Application Logic
 * 
 * Handles:
 * - Direct WebSocket connection to Gemini Live API (via ephemeral token)
 * - Mic capture (getUserMedia → AudioWorklet → PCM 16kHz)
 * - Audio playback (PCM 24kHz → Web Audio API)
 * - Interruption handling (flush audio on interrupted signal)
 * - Live transcript display
 */

// ════════════════════════════════════════════════════════════════
// Constants
// ════════════════════════════════════════════════════════════════

const GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025";
// Use BidiGenerateContent (not Constrained) — Constrained doesn't support function calling
const GEMINI_WS_BASE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent";
const INPUT_SAMPLE_RATE = 16000;
const OUTPUT_SAMPLE_RATE = 24000;
const ENERGY_THRESHOLD = 0.005;

// ════════════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════════════

const state = {
    ws: null,
    audioCapture: null,
    audioPlayer: null,
    sessionActive: false,
    sessionStartTime: null,
    timerInterval: null,

    audioState: 'IDLE',
    audioBuffer: [],

    // Transcript state for streaming/appending
    currentUserMsg: null,
    currentAssistantMsg: null,

    // Codebase intelligence (Weekend 2)
    toolDeclarations: null,  // Fetched from backend
    codebaseSummary: null,   // Injected into system prompt
    pendingToolCalls: 0,     // Track in-flight tool calls

    // Debug
    audioChunksSent: 0,
    audioChunksReceived: 0,
    debugEvents: [],
    fullTrace: [],        // Full trace with ms-level timing
    sessionStartMs: 0,    // performance.now() at session start
    sessionLogger: null,  // Persistent logger instance
    lastToolCall: null,
    lastToolResult: null,
    lastServerContentKeys: null,
    lastTopLevelKeys: null,
    lastInboundAtMs: null,
    silenceTimer: null,
};

// ════════════════════════════════════════════════════════════════
// Session Management
// ════════════════════════════════════════════════════════════════

async function toggleSession() {
    if (state.sessionActive) {
        await endSession();
    } else {
        await startSession();
    }
}

async function startSession() {
    updateStatus("connecting", "Scanning workspace...");
    const rulesList = document.getElementById("rules-list");
    if (rulesList) rulesList.innerHTML = "";

    try {
        const storedHandle = localStorage.getItem('codiey_resumption_token');
        state.isResuming = !!storedHandle;

        // 1. Signal session start (resets mental model) + fetch all startup data in parallel
        const [, declRes, summaryRes, keyRes] = await Promise.all([
            fetch("/api/session/start", { method: "POST" }),
            fetch("/api/tools/declarations"),
            fetch("/api/workspace/summary"),
            fetch("/api/key"),
        ]);

        if (!keyRes.ok) {
            const err = await keyRes.json();
            throw new Error(err.detail || "Failed to fetch API key");
        }

        const { key } = await keyRes.json();

        // Store tool declarations
        if (declRes.ok) {
            state.toolDeclarations = await declRes.json();
            console.log("🔧 Tool declarations loaded:", state.toolDeclarations);
        }

        // Store codebase summary (rules already injected first by summary_builder)
        if (summaryRes.ok) {
            const summaryData = await summaryRes.json();
            state.codebaseSummary = summaryData.summary;
            console.log(`📁 Codebase summary loaded (${state.codebaseSummary.length} chars)`);
        }

        updateStatus("connecting", "Connecting to Gemini...");

        // 2. Open direct WebSocket to Gemini (using API key, not ephemeral token)
        const wsUrl = `${GEMINI_WS_BASE_URL}?key=${key}`;
        state.ws = new WebSocket(wsUrl);

        state.ws.onopen = () => {
            console.log("✅ WebSocket connected to Gemini");
            sendSetupMessage();
        };

        state.ws.onmessage = async (event) => {
            let data;
            if (event.data instanceof Blob) {
                data = JSON.parse(await event.data.text());
            } else {
                data = JSON.parse(event.data);
            }
            handleGeminiMessage(data);
        };

        state.ws.onerror = (err) => {
            console.error("WebSocket error:", err);
            updateStatus("disconnected", "Connection error");
            // Log the error persistently
            if (state.sessionLogger) {
                state.sessionLogger.log('WS_ERROR', 'WebSocket error event', { error: String(err) });
                state.sessionLogger.flush();
            }
        };

        state.ws.onclose = (event) => {
            handleWebSocketClose(event);
        };

    } catch (err) {
        console.error("Failed to start session:", err);
        updateStatus("disconnected", `Error: ${err.message}`);
        addSystemMessage(`Error: ${err.message}`);
    }
}

function sendSetupMessage() {
    // System prompt assembly order (highest attention first):
    //   1. Rules file content  — verified project truths, already at the top of codebaseSummary
    //   2. Directory tree      — project structure, also inside codebaseSummary
    //   3. Fixed instruction block — under 300 tokens, no per-tool descriptions

    const FIXED_INSTRUCTIONS = `You are Codiey, a voice-first codebase thinking partner. You help developers reason about code through conversation. You never write or modify code.

Speak only in English. Keep responses to 2-3 sentences, then wait. Ask clarifying questions instead of monologuing. Use natural filler ("hmm", "right", "so") and pause between thoughts.

When interrupted: acknowledge briefly, confirm the new direction, connect old and new topics only if genuinely relevant, continue. Never restart cold. Always carry context forward.

Never use uncertain language — 'likely', 'probably', 'appears to be', 'suggests' — when a tool call can verify the answer. If you are uncertain, say "let me check" and use a tool. Never guess.

Tools: grep_search, file_search, list_directory, read_file, get_function_info, mark_as_discussed, write_to_rules

Whenever you learn a stable, reusable fact about the user or codebase, call write_to_rules immediately with a single-sentence insight. Do this proactively without being asked. Do not repeat existing rules.`;

    // codebaseSummary already has rules first, then directory tree (from summary_builder.py)
    const projectContext = state.codebaseSummary
        ? `${state.codebaseSummary}\n`
        : "";

    const systemPrompt = `${projectContext}${FIXED_INSTRUCTIONS}`;

    const setupMessage = {
        setup: {
            model: `models/${GEMINI_MODEL}`,
            generationConfig: {
                responseModalities: ["AUDIO"],
                speechConfig: {
                    voiceConfig: {
                        prebuiltVoiceConfig: { voiceName: "Kore" }
                    }
                }
            },
            systemInstruction: {
                parts: [{ text: systemPrompt }]
            },
            realtimeInputConfig: {
                automaticActivityDetection: {
                    disabled: false,
                    startOfSpeechSensitivity: "START_SENSITIVITY_LOW",
                    endOfSpeechSensitivity: "END_SENSITIVITY_LOW",
                    prefixPaddingMs: 20,
                    silenceDurationMs: 600
                }
            },
            inputAudioTranscription: {},
            outputAudioTranscription: {},
            contextWindowCompression: {
                slidingWindow: {}
            },
            // ── Tool declarations (Weekend 2) ──
            tools: state.toolDeclarations || [],
        }
    };
    // Automatically request a resumption handle, even if we don't have one yet
    setupMessage.setup.sessionResumption = {};
    
    const storedHandle = localStorage.getItem('codiey_resumption_token');
    if (storedHandle) {
        setupMessage.setup.sessionResumption.handle = storedHandle;
    }

    state.ws.send(JSON.stringify(setupMessage));
    console.log("📤 Setup message sent (with tools + codebase summary)");
    if (state.sessionLogger) {
        const toolNames = getDeclaredToolNames();
        state.sessionLogger.log('SETUP_SENT', 'Setup sent', {
            model: GEMINI_MODEL,
            hasTools: toolNames.length > 0,
            toolCount: toolNames.length,
            toolNames,
            hasSummary: !!state.codebaseSummary,
        });
    }
}

function handleWebSocketClose(event) {
    // Don't trigger during intentional reconnect
    if (state.reconnecting) return;

    console.log("WebSocket closed:", event.code, event.reason);

    if (state.sessionLogger) {
        state.sessionLogger.log('WS_CLOSE', `WebSocket closed: ${event.code} ${event.reason}`, {
            code: event.code,
            reason: event.reason,
            wasClean: event.wasClean,
            lastToolCall: state.lastToolCall,
            lastToolResult: state.lastToolResult,
            lastServerContentKeys: state.lastServerContentKeys,
            lastTopLevelKeys: state.lastTopLevelKeys,
            lastInboundAtMs: state.lastInboundAtMs,
            audioState: state.audioState,
        });
        state.sessionLogger.flush();
    }

    if (event.code === 1008 && state.reconnecting) {
        debugLog('warn', 'RECONNECT', 'Resumption handle rejected (expired?) — starting fresh session');
        localStorage.removeItem('codiey_resumption_token');
        state.reconnecting = false;
        // Could auto-start a fresh session here, or just end and let user restart
        endSession();
        addSystemMessage('Session expired — please start a new session');
        return;
    }

    // If session was active and we have a resumption handle, try auto-reconnect
    // Don't reconnect on clean close (1000) — that's intentional
    const handle = localStorage.getItem('codiey_resumption_token');
    if (state.sessionActive && handle && event.code !== 1000) {
        debugLog('warn', 'RECONNECT', `Unexpected close (${event.code}), attempting reconnect...`);
        addSystemMessage('Connection lost — reconnecting...');
        attemptGracefulReconnect();
        return;
    }

    if (state.sessionActive) {
        endSession();
        addSystemMessage("Connection lost");
    }
}

async function attemptGracefulReconnect() {
    const handle = localStorage.getItem('codiey_resumption_token');
    if (!handle) {
        debugLog('warn', 'RECONNECT', 'No resumption handle — cannot reconnect');
        return;
    }

    state.reconnecting = true;

    // Close current WebSocket cleanly (don't trigger endSession)
    if (state.ws) {
        state.ws.onclose = null; // Detach handler to prevent endSession
        state.ws.close(1000, 'Graceful reconnect');
        state.ws = null;
    }

    try {
        const keyRes = await fetch('/api/key');
        if (!keyRes.ok) throw new Error('Failed to fetch API key');
        const { key } = await keyRes.json();

        const wsUrl = `${GEMINI_WS_BASE_URL}?key=${key}`;
        state.ws = new WebSocket(wsUrl);

        state.ws.onopen = () => {
            debugLog('ok', 'RECONNECT', 'WebSocket reconnected — sending setup with resumption handle');
            sendSetupMessage(); // Will include the handle from localStorage
        };

        state.ws.onmessage = async (event) => {
            let data;
            if (event.data instanceof Blob) {
                data = JSON.parse(await event.data.text());
            } else {
                data = JSON.parse(event.data);
            }
            handleGeminiMessage(data);
        };

        state.ws.onerror = (err) => {
            console.error('Reconnection WebSocket error:', err);
            state.reconnecting = false;
        };

        state.ws.onclose = (event) => {
            handleWebSocketClose(event);
        };

        state.reconnecting = false;

    } catch (err) {
        console.error('Reconnection failed:', err);
        state.reconnecting = false;
        addSystemMessage('Reconnection failed — session ended');
        endSession();
    }
}

async function endSession(keepAudio = false) {
    // Only tear down audio if this is a real session end, not a reconnect
    if (!keepAudio) {
        // Stop audio capture immediately so the mic releases
        if (state.audioCapture) {
            state.audioCapture.stop();
            state.audioCapture = null;
        }

        // Flush and close audio player
        if (state.audioPlayer) {
            state.audioPlayer.flush();
            state.audioPlayer.audioContext.close();
            state.audioPlayer = null;
        }
    }

    // Log session end before flushing
    if (state.sessionLogger) {
        state.sessionLogger.log('SESSION_END', 'Session ended');
        await state.sessionLogger.flush();
    }

    // Drain pending Tier 2 background tasks on the backend (2s timeout)
    // This ensures write_to_rules / mark_as_discussed finish before the session closes
    try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 2500);
        await fetch("/api/session/end", {
            method: "POST",
            signal: controller.signal,
        });
        clearTimeout(timeoutId);
    } catch (err) {
        // Timeout or network error — proceed with cleanup anyway
        console.warn("session/end drain did not complete:", err.message);
    }

    // Close WebSocket after backend has drained
    if (state.ws) {
        state.ws.close();
        state.ws = null;
    }

    // Stop session logger (final flush already done above)
    if (state.sessionLogger) {
        state.sessionLogger.stop();
        state.sessionLogger = null;
    }

    // Reset UI
    state.sessionActive = false;
    state.currentUserMsg = null;
    state.currentAssistantMsg = null;

    if (state.timerInterval) {
        clearInterval(state.timerInterval);
        state.timerInterval = null;
    }

    state.audioState = 'IDLE';
    state.audioBuffer = [];

    updateStatus("disconnected", "Session ended");
    updateSessionButton(false);
    showWelcome(true);

    addSystemMessage("Session ended");
}

// ════════════════════════════════════════════════════════════════
// Debug Helpers
// ════════════════════════════════════════════════════════════════

function debugLog(emoji, category, detail) {
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false, fractionalSecondDigits: 1 });
    const elapsedMs = state.sessionStartMs ? Math.round(performance.now() - state.sessionStartMs) : 0;
    const msg = `${emoji} [${ts}] ${category}: ${detail}`;
    console.log(msg);

    // Full trace (for export)
    state.fullTrace.push({ elapsedMs, category, detail, ts });

    // Persistent session log (streams to backend)
    if (state.sessionLogger) {
        state.sessionLogger.log(category, detail);
    }

    // Keep last 20 events for UI
    state.debugEvents.push({ ts, emoji, category, detail, elapsedMs });
    if (state.debugEvents.length > 20) state.debugEvents.shift();

    // Update debug panel
    updateDebugPanel();
}

function updateDebugPanel() {
    const panel = document.getElementById('debug-log');
    if (!panel) return;
    const last8 = state.debugEvents.slice(-8);
    panel.innerHTML = last8.map(e =>
        `<div class="debug-entry"><span class="debug-ts">${e.elapsedMs}ms</span> ${e.emoji} <span class="debug-cat">${e.category}</span> ${e.detail}</div>`
    ).join('');
    panel.scrollTop = panel.scrollHeight;
}

async function saveTraces() {
    const traceData = {
        sessionDuration: state.sessionStartMs ? Math.round(performance.now() - state.sessionStartMs) : 0,
        totalAudioChunksSent: state.audioChunksSent,
        totalAudioChunksReceived: state.audioChunksReceived,
        events: state.fullTrace,
    };

    try {
        const res = await fetch('/api/traces', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(traceData)
        });
        if (res.ok) {
            debugLog('💾', 'TRACES', 'Saved to .codiey/traces.json');
        }
    } catch (e) {
        console.error('Failed to save traces:', e);
    }
}

// ════════════════════════════════════════════════════════════════
// Gemini Message Handler
// ════════════════════════════════════════════════════════════════

function handleGeminiMessage(data) {
    state.lastTopLevelKeys = Object.keys(data);
    state.lastInboundAtMs = Math.round(performance.now());

    // Setup complete — start capturing audio
    if (data.setupComplete) {
        debugLog('🏁', 'SETUP', 'Setup complete from Gemini');
        onSessionReady();
        return;
    }

    const sc = data.serverContent;
    if (sc) {
        state.lastServerContentKeys = Object.keys(sc);
        // 1. Interruption — flush audio immediately
        if (sc.interrupted) {
            debugLog('�', 'INTERRUPT', 'Model was interrupted by user speech');
            if (state.audioPlayer) state.audioPlayer.flush();
            state.currentAssistantMsg = null;
            addSystemMessage("Interrupted");
            // NOTE: Do NOT return — other fields may be present on this message
        }

        // 2. Model audio output
        if (sc.modelTurn && sc.modelTurn.parts) {
            if (state.audioState === 'IDLE') {
                state.audioState = 'STREAMING'; // Allow barge-in during model speech
            }
            for (const part of sc.modelTurn.parts) {
                if (part.inlineData && part.inlineData.data) {
                    // Reset silence timer on every audio chunk
                    if (state.silenceTimer) clearTimeout(state.silenceTimer);
                    state.silenceTimer = setTimeout(() => {
                        if (state.audioState === 'STREAMING') {
                            state.audioState = 'IDLE';
                            debugLog('🔇', 'AUDIO_GATE', 'Model audio silent — mic gated');
                        }
                    }, 300);

                    state.audioChunksReceived++;
                    if (state.audioChunksReceived % 10 === 1) {
                        const sizeKB = (part.inlineData.data.length * 0.75 / 1024).toFixed(1);
                        debugLog('🔊', 'AUDIO_OUT', `Chunk #${state.audioChunksReceived} (${sizeKB}KB) mimeType=${part.inlineData.mimeType || 'pcm'}`);
                    }
                    if (state.audioPlayer) {
                        state.audioPlayer.play(part.inlineData.data);
                    }
                }
            }
        }

        // 3. Input transcription (what user said)
        if (sc.inputTranscription) {
            const text = sc.inputTranscription.text || "";
            const finished = sc.inputTranscription.finished || false;
            if (text.trim()) {
                debugLog('🎤', 'USER_SPEECH', `"${text.trim()}" (finished=${finished})`);
                if (!state.currentUserMsg) {
                    state.currentUserMsg = addMessage("", "user");
                }
                state.currentUserMsg.textContent += text;
                scrollTranscript();
            }
            if (finished) {
                state.currentUserMsg = null;
            }
        }

        // 4. Output transcription (what Gemini said)
        if (sc.outputTranscription) {
            const text = sc.outputTranscription.text || "";
            const finished = sc.outputTranscription.finished || false;
            if (text.trim()) {
                setSessionState("speaking");
                debugLog('🤖', 'AI_SPEECH', `"${text.trim()}" (finished=${finished})`);
                if (!state.currentAssistantMsg) {
                    state.currentAssistantMsg = addMessage("", "assistant");
                }
                state.currentAssistantMsg.textContent += text;
                scrollTranscript();
            }
            if (finished) {
                state.currentAssistantMsg = null;
            }
        }

        // 5. Turn complete
        if (sc.turnComplete) {
            debugLog('✅', 'TURN', 'Turn complete');
            if (state.silenceTimer) clearTimeout(state.silenceTimer);
            state.currentAssistantMsg = null;
            state.audioState = 'IDLE'; // Start silent gating
            setSessionState("listening");
        }

        if (sc.generationComplete) {
            debugLog('🏁', 'GEN', 'Generation complete');
            state.audioState = 'IDLE'; // ← this line stops the mic before tool call arrives
        }

        // Log any unhandled serverContent keys
        const knownKeys = ['interrupted', 'modelTurn', 'inputTranscription', 'outputTranscription', 'turnComplete', 'groundingMetadata', 'generationComplete'];
        const unknownKeys = Object.keys(sc).filter(k => !knownKeys.includes(k));
        if (unknownKeys.length > 0) {
            debugLog('❓', 'UNKNOWN', `serverContent keys: ${unknownKeys.join(', ')} = ${JSON.stringify(sc)}`);
        }
    }

    // ── Tool calls ──
    if (data.toolCall) {
        // Log the raw tool call for debugging
        if (state.sessionLogger) {
            state.sessionLogger.log('TOOL_CALL_RAW', 'Raw toolCall from Gemini', { toolCall: data.toolCall });
            state.sessionLogger.flush(); // Flush immediately — this is where crashes happen
        }
        handleToolCall(data.toolCall);
    }
    if (data.toolCallCancellation) {
        debugLog('warn', 'TOOL_CANCEL', `Tool call cancelled: ${JSON.stringify(data.toolCallCancellation)}`);
        if (state.sessionLogger) {
            state.sessionLogger.log('TOOL_CANCEL', 'Tool call cancelled', { toolCallCancellation: data.toolCallCancellation });
        }
    }

    // Log any completely unrecognized top-level keys
    const knownTopKeys = ['setupComplete', 'serverContent', 'toolCall', 'toolCallCancellation', 'sessionResumptionUpdate', 'usageMetadata', 'goAway'];
    const unknownTopKeys = Object.keys(data).filter(k => !knownTopKeys.includes(k));
    if (unknownTopKeys.length > 0) {
        debugLog('❓', 'UNKNOWN_MSG', `Unknown top-level keys: ${unknownTopKeys.join(', ')}`);
        if (state.sessionLogger) {
            state.sessionLogger.log('UNKNOWN_MSG', 'Unknown message from Gemini', { data });
        }
    }

    // Session resumption token
    if (data.sessionResumptionUpdate) {
        const update = data.sessionResumptionUpdate;
        if (update.resumeToken) {
            console.log("🔄 Got resumption token");
            localStorage.setItem("codiey_resumption_token", update.resumeToken);
        }
    }

    if (data.goAway) {
        const timeLeft = data.goAway.timeLeft || 'unknown';
        debugLog('warn', 'GOAWAY', `Server will close connection in ${timeLeft}`);

        if (state.sessionLogger) {
            state.sessionLogger.log('GOAWAY', `Server will close in ${timeLeft}`, { timeLeft });
        }

        // Proactively reconnect before forced close
        addSystemMessage('Reconnecting seamlessly...');
        attemptGracefulReconnect();
    }
}

function onSessionReady() {
    state.sessionActive = true;
    state.sessionStartTime = Date.now();
    
    // Clear the token if this is a fresh session start without resumption
    if (!state.isResuming) {
        localStorage.removeItem('codiey_resumption_token');
    }
    state.sessionStartMs = performance.now();
    state.audioChunksSent = 0;
    state.audioChunksReceived = 0;
    state.fullTrace = [];

    state.audioState = 'IDLE';
    state.audioBuffer = [];

    // Start persistent session logger
    state.sessionLogger = new SessionLogger();
    state.sessionLogger.log('SESSION_START', 'Session started', {
        model: GEMINI_MODEL,
        hasTools: !!state.toolDeclarations,
        hasSummary: !!state.codebaseSummary,
        summaryLength: state.codebaseSummary ? state.codebaseSummary.length : 0,
    });

    // Create audio player NOW (after user gesture) so AudioContext is allowed
    state.audioPlayer = new AudioPlayer();

    updateStatus("connected", "Connected");
    updateSessionButton(true);
    showWelcome(false);
    startTimer();
    setSessionState("listening");

    // Set project name from codebase summary (first line or fallback)
    if (state.codebaseSummary) {
        const firstLine = state.codebaseSummary.split('\n')[0] || '';
        const name = firstLine.replace(/^#\s*/, '').trim().substring(0, 40) || 'project';
        document.getElementById("project-name").textContent = name;
    }

    addSystemMessage("Session started — speak to begin");

    // Start audio capture
    startAudioCapture();
}

// ════════════════════════════════════════════════════════════════
// Audio Capture (Mic → Gemini)
// ════════════════════════════════════════════════════════════════

async function startAudioCapture() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                sampleRate: INPUT_SAMPLE_RATE,
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
            }
        });

        const audioContext = new AudioContext({ sampleRate: INPUT_SAMPLE_RATE });
        const source = audioContext.createMediaStreamSource(stream);

        await audioContext.audioWorklet.addModule("/static/pcm-processor.js");
        const workletNode = new AudioWorkletNode(audioContext, "pcm-processor");

        workletNode.port.onmessage = (event) => {
            if (event.data.type !== "pcm" || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;

            const { data, energy } = event.data;

            switch (state.audioState) {
                case 'STREAMING':
                    sendAudioChunk(data);
                    break;

                case 'IDLE':
                    // Silent gating: only resume when voice detected
                    if (energy > ENERGY_THRESHOLD) {
                        state.audioState = 'STREAMING';
                        sendAudioChunk(data);
                    }
                    // Otherwise: discard silent chunk
                    break;

                case 'TOOL_PENDING':
                    // Ignore mic input during tool calls — no interruption until tool completes
                    break;
            }
        };

        source.connect(workletNode);
        workletNode.connect(audioContext.destination); // Required for worklet to process

        state.audioCapture = {
            stream,
            audioContext,
            source,
            workletNode,
            stop() {
                workletNode.disconnect();
                source.disconnect();
                stream.getTracks().forEach(t => t.stop());
                audioContext.close();
            }
        };

        document.getElementById("waveform").classList.add("active");

        console.log("🎤 Audio capture started");

    } catch (err) {
        console.error("Mic access failed:", err);
        addSystemMessage(`Microphone error: ${err.message}`);
    }
}

function sendAudioChunk(pcmArrayBuffer) {
    const base64 = arrayBufferToBase64(pcmArrayBuffer);
    state.ws.send(JSON.stringify({
        realtimeInput: {
            audio: {
                data: base64,
                mimeType: "audio/pcm;rate=16000"
            }
        }
    }));
    state.audioChunksSent++;
    if (state.audioChunksSent % 50 === 1) {
        debugLog('📤', 'AUDIO_IN', `Sent mic chunk #${state.audioChunksSent} (${base64.length} chars b64)`);
    }
}

// ════════════════════════════════════════════════════════════════
// Persistent Session Logger
// ════════════════════════════════════════════════════════════════

class SessionLogger {
    constructor() {
        this.buffer = [];
        this.sessionId = new Date().toISOString().replace(/[:.]/g, '-');
        this.startTime = performance.now();

        // Flush every 2 seconds
        this.flushInterval = setInterval(() => this.flush(), 2000);

        // Flush on page unload (last resort)
        this._onBeforeUnload = () => this.flush(true);
        window.addEventListener('beforeunload', this._onBeforeUnload);
    }

    log(category, detail, extra = null) {
        const entry = {
            ts: new Date().toISOString(),
            elapsedMs: Math.round(performance.now() - this.startTime),
            category,
            detail,
        };
        if (extra) entry.extra = extra;
        this.buffer.push(entry);
    }

    async flush(useBeacon = false) {
        if (this.buffer.length === 0) return;

        const entries = this.buffer.splice(0);

        const payload = JSON.stringify({
            sessionId: this.sessionId,
            entries,
        });

        if (useBeacon) {
            // navigator.sendBeacon is fire-and-forget, works during unload
            navigator.sendBeacon('/api/session-log', new Blob([payload], { type: 'application/json' }));
        } else {
            try {
                await fetch('/api/session-log', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: payload,
                });
            } catch (e) {
                // Put entries back if flush failed
                this.buffer.unshift(...entries);
                console.warn('Session log flush failed:', e);
            }
        }
    }

    stop() {
        clearInterval(this.flushInterval);
        window.removeEventListener('beforeunload', this._onBeforeUnload);
        this.flush(); // Final flush
    }
}

// ════════════════════════════════════════════════════════════════
// Audio Player (Gemini → Speakers)
// ════════════════════════════════════════════════════════════════

class AudioPlayer {
    constructor() {
        this.audioContext = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE });
        // Resume immediately — this is called after a user gesture (click Start)
        this.audioContext.resume();
        this.queue = [];
        this.nextStartTime = 0;
        console.log("🔊 AudioPlayer created, context state:", this.audioContext.state);
    }

    async play(base64PCM) {
        // Ensure context is running
        if (this.audioContext.state === "suspended") {
            await this.audioContext.resume();
        }

        const pcmData = base64ToInt16Array(base64PCM);
        const float32 = new Float32Array(pcmData.length);

        for (let i = 0; i < pcmData.length; i++) {
            float32[i] = pcmData[i] / 32768.0;
        }

        const buffer = this.audioContext.createBuffer(1, float32.length, OUTPUT_SAMPLE_RATE);
        buffer.copyToChannel(float32, 0);

        const source = this.audioContext.createBufferSource();
        source.buffer = buffer;
        source.connect(this.audioContext.destination);

        // Schedule seamlessly after previous chunk
        const now = this.audioContext.currentTime;
        const startTime = Math.max(now, this.nextStartTime);
        source.start(startTime);
        this.nextStartTime = startTime + buffer.duration;

        this.queue.push(source);

        source.onended = () => {
            const idx = this.queue.indexOf(source);
            if (idx > -1) this.queue.splice(idx, 1);
        };
    }

    flush() {
        // Stop all queued and playing audio immediately
        for (const source of this.queue) {
            try { source.stop(); } catch (e) { /* already stopped */ }
        }
        this.queue = [];
        this.nextStartTime = 0;
        console.log("🔇 Audio flushed");
    }
}

// NOTE: AudioPlayer is created in onSessionReady() — NOT here.
// Creating it here (before user gesture) causes browser to suspend the AudioContext.

// ════════════════════════════════════════════════════════════════
// Tool Call Handler (Weekend 2)
// ════════════════════════════════════════════════════════════════

// Tier 2 tools: fire-and-forget. Backend returns {"status":"queued"} instantly.
// Gemini receives the response with SILENT scheduling so it never speaks about
// having called these tools. No UI indicator is shown either.
const TIER2_SILENT_TOOLS = new Set(['mark_as_discussed', 'write_to_rules']);

function getDeclaredToolNames() {
    const names = [];
    const decls = state.toolDeclarations || [];
    for (const entry of decls) {
        if (entry && Array.isArray(entry.functionDeclarations)) {
            for (const fn of entry.functionDeclarations) {
                if (fn && fn.name) names.push(fn.name);
            }
        } else if (entry && entry.name) {
            names.push(entry.name);
        }
    }
    return names;
}

async function handleToolCall(toolCall) {
    const functionCalls = toolCall.functionCalls || [];
    debugLog('🔧', 'TOOL_CALL', `${functionCalls.length} tool(s): ${functionCalls.map(fc => fc.name).join(', ')}`);

    setSessionState("analyzing");

    state.lastToolCall = {
        atMs: Math.round(performance.now()),
        names: functionCalls.map(fc => fc.name),
        args: functionCalls.map(fc => Object.keys(fc.args || {})),
    };

    const declared = new Set(getDeclaredToolNames());
    const unknownCalls = functionCalls.filter(fc => !declared.has(fc.name));
    if (unknownCalls.length) {
        const names = unknownCalls.map(fc => fc.name).join(', ');
        debugLog('warn', 'TOOL_UNKNOWN', `Undeclared tool(s): ${names}`);
        if (state.sessionLogger) {
            state.sessionLogger.log('TOOL_UNKNOWN', 'Undeclared tool(s) requested', { names, toolCall });
        }
    }

    // Wire write_to_rules tool to addRule()
    for (const fc of functionCalls) {
        if (fc.name === 'write_to_rules' && fc.args && fc.args.insight) {
            addRule(fc.args.insight);
        }
    }

    let entersToolPending = false;

    // Show UI indicator for Tier 1 tools only; suppress Tier 2 side-effect tools
    for (const fc of functionCalls) {
        if (TIER2_SILENT_TOOLS.has(fc.name)) continue;
        entersToolPending = true;
        // Strip 'reasoning' from display — it's an internal schema field
        const displayArgs = fc.args
            ? Object.entries(fc.args)
                .filter(([k]) => k !== 'reasoning')
                .map(([k, v]) => `${k}=${v}`)
                .join(', ')
            : '';
        addToolMessage(`🔧 ${fc.name}(${displayArgs})`);
        debugLog('🔧', 'TOOL_ARGS', `${fc.name} args=[${displayArgs}]`);
    }

    if (entersToolPending) {
        state.audioState = 'TOOL_PENDING';
    }

    // Execute all tool calls in parallel via the backend
    const responses = await Promise.all(
        functionCalls.map(async (fc) => {
            try {
                const res = await fetch('/api/tools/execute', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ tool_name: fc.name, args: fc.args || {} })
                });

                if (!res.ok) {
                    debugLog('warn', 'TOOL_HTTP', `${fc.name} HTTP ${res.status}`);
                }

                const result = await res.json();
                state.lastToolResult = { name: fc.name, result };
                debugLog('✅', 'TOOL_RESULT', `${fc.name} → ${JSON.stringify(result).substring(0, 100)}...`);

                if (result && result.error) {
                    debugLog('warn', 'TOOL_ERROR', `${fc.name} error: ${result.error}`);
                    if (state.sessionLogger) {
                        state.sessionLogger.log('TOOL_ERROR', 'Tool returned error', { name: fc.name, error: result.error });
                    }
                }

                // Extract filename from tool args/results for graph visualization
                const toolFilename = fc.args?.path || fc.args?.filename || fc.args?.file || fc.args?.name || null;
                if (toolFilename) {
                    const shortName = toolFilename.split('/').pop().split('\\').pop();
                    const prevActive = Object.keys(graphState.nodes).find(k => graphState.nodes[k].state === 'active');
                    const rx = 30 + Math.random() * 190;
                    const ry = 20 + Math.random() * 280;
                    addGraphNode(shortName, "visited", rx, ry);
                    if (prevActive && prevActive !== shortName) {
                        addGraphEdge(prevActive, shortName);
                    }
                    setActiveNode(shortName);
                }

                return {
                    id: fc.id,
                    name: fc.name,
                    response: { result: result }
                };
            } catch (err) {
                console.error(`Tool ${fc.name} failed:`, err);
                return {
                    id: fc.id,
                    name: fc.name,
                    response: { result: { error: `Tool execution failed: ${err.message}` } }
                };
            }
        })
    );

    // If the user spoke during our TOOL_PENDING state, the audioState will
    // have naturally transitioned out of TOOL_PENDING to STREAMING.
    // In that case, we MUST respond with SILENT so Gemini doesn't speak the result 
    // over the user's ongoing speech.
    let globalScheduling = 'WHEN_IDLE';
    if (entersToolPending && state.audioState !== 'TOOL_PENDING') {
        globalScheduling = 'SILENT';
        debugLog('warn', 'TOOL', 'User barged in during tool call — sending SILENT response');
    } else if (entersToolPending) {
        // User didn't interrupt, proceed normally
        state.audioState = 'STREAMING';
    }

    // Apply the correct scheduling per tool type and global state
    for (const r of responses) {
        if (TIER2_SILENT_TOOLS.has(r.name)) {
            r.response.scheduling = 'SILENT';
        } else {
            r.response.scheduling = globalScheduling;
        }
    }

    sendToolResponseToGemini(responses);
}

function sendToolResponseToGemini(functionResponses) {
    const msg = {
        toolResponse: {
            functionResponses: functionResponses
        }
    };
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify(msg));
        debugLog('📤', 'TOOL_RESPONSE', `Sent ${functionResponses.length} response(s) to Gemini`);
    }
}

// ════════════════════════════════════════════════════════════════
// UI Helpers
// ════════════════════════════════════════════════════════════════

function addMessage(text, type) {
    const container = document.getElementById("messages-container");
    const div = document.createElement("div");
    div.className = `message ${type}`;
    div.textContent = text;
    container.appendChild(div);
    scrollTranscript();
    return div;
}

function addSystemMessage(text) {
    addMessage(`[${text}]`, "system");
}

function addToolMessage(text) {
    const container = document.getElementById("tool-toast-container");
    const toast = document.createElement("div");
    toast.className = "tool-toast";
    toast.innerHTML = `<div class="tool-toast-icon">⌕</div><span class="tool-toast-name">${text}</span>`;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

function scrollTranscript() {
    const container = document.getElementById("messages-area");
    container.scrollTop = container.scrollHeight;
}

function updateStatus(status, text) {
    const dot = document.getElementById("status-dot");
    const label = document.getElementById("status-text");

    dot.className = `status-dot status-${status}`;
    label.textContent = text;
}

function updateSessionButton(active) {
    const btn = document.getElementById("session-btn");
    if (active) {
        btn.textContent = "End Session";
        btn.classList.remove("hidden");
    } else {
        btn.textContent = "Start Session";
        btn.classList.add("hidden");
    }
}

function showWelcome(show) {
    document.getElementById("welcome-screen").classList.toggle("hidden", !show);

    if (!show) {
        document.getElementById("messages-container").innerHTML = "";
    }
}

function startTimer() {
    const timerEl = document.getElementById("session-timer");
    state.timerInterval = setInterval(() => {
        if (!state.sessionStartTime) return;
        const elapsed = Math.floor((Date.now() - state.sessionStartTime) / 1000);
        const mins = Math.floor(elapsed / 60);
        const secs = elapsed % 60;
        timerEl.textContent = `${mins}:${secs.toString().padStart(2, "0")}`;
    }, 1000);
}

// ════════════════════════════════════════════════════════════════
// Utility
// ════════════════════════════════════════════════════════════════

function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}

function base64ToInt16Array(base64) {
    const binaryString = atob(base64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
    }
    return new Int16Array(bytes.buffer);
}

// ════════════════════════════════════════════════════════════════
// Init
// ════════════════════════════════════════════════════════════════

// ════════════════════════════════════════════════════════════════
// Graph, Rules, and Session State (UI layer)
// ════════════════════════════════════════════════════════════════

const graphState = {
    nodes: {},  // filename -> { el, x, y, state }
    nodeCount: 0,
};

function addGraphNode(filename, nodeState, x, y) {
    if (graphState.nodes[filename]) return;

    const container = document.getElementById("graph-nodes");
    const el = document.createElement("div");
    el.className = `graph-node ${nodeState}`;
    el.dataset.filename = filename;
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
    el.style.animation = 'nodeAppear 0.6s ease-out both';
    el.innerHTML = `<span class="node-dot"></span><span class="node-label">${filename}</span>`;
    container.appendChild(el);

    graphState.nodes[filename] = { el, x, y, state: nodeState };
    graphState.nodeCount++;

    const badge = document.getElementById("node-count-badge");
    badge.textContent = `${graphState.nodeCount} node${graphState.nodeCount !== 1 ? 's' : ''}`;
}

function setActiveNode(filename) {
    const nodes = document.querySelectorAll("#graph-nodes .graph-node");
    nodes.forEach(n => {
        if (n.classList.contains("active")) {
            n.classList.remove("active");
            n.classList.add("visited");
        }
    });

    const entry = graphState.nodes[filename];
    if (entry) {
        entry.el.className = "graph-node active";
        entry.state = "active";
    }

    const bar = document.getElementById("breadcrumb-bar");
    const parts = filename.replace(/[()]/g, '').split(/[\/\\\.]/);
    const segments = parts.map((p, i) => {
        if (i === parts.length - 1) {
            return `<span class="breadcrumb-active">${filename}</span>`;
        }
        return `<span class="breadcrumb-segment">${p}</span><span class="breadcrumb-sep">›</span>`;
    });
    bar.innerHTML = segments.join('');
}

function addGraphEdge(fromFile, toFile) {
    const from = graphState.nodes[fromFile];
    const to = graphState.nodes[toFile];
    if (!from || !to) return;

    const svg = document.getElementById("graph-svg");
    const ns = "http://www.w3.org/2000/svg";
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", from.x + 5);
    line.setAttribute("y1", from.y + 5);
    line.setAttribute("x2", to.x + 5);
    line.setAttribute("y2", to.y + 5);
    line.setAttribute("class", "graph-edge");
    line.setAttribute("marker-end", "url(#arrow)");
    svg.appendChild(line);
}

function addRule(text) {
    const list = document.getElementById("rules-list");
    const item = document.createElement("div");
    item.className = "rule-item flash";
    item.innerHTML = `<span class="rule-dot"></span><span class="rule-text">${text}</span>`;
    list.prepend(item);
    setTimeout(() => item.classList.remove("flash"), 2000);
}

function setSessionState(sessionState) {
    const dot = document.getElementById("status-dot");
    const text = document.getElementById("status-text");
    const orb = document.getElementById("orb");
    const waveform = document.getElementById("waveform");

    orb.className = "orb-container";

    switch (sessionState) {
        case "listening":
            dot.className = "status-dot status-connected";
            text.textContent = "LISTENING";
            orb.classList.add("listening");
            waveform.classList.remove("active");
            break;
        case "speaking":
            dot.className = "status-dot status-connected";
            text.textContent = "CODIEY SPEAKING";
            orb.classList.add("speaking");
            waveform.classList.add("active");
            break;
        case "analyzing":
            dot.className = "status-dot status-connected";
            text.textContent = "ANALYZING";
            orb.classList.add("analyzing");
            waveform.classList.remove("active");
            break;
    }
}

console.log("🎙️ Codiey loaded");
