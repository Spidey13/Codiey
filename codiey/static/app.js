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
    isUserSpeaking: false,

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
        
        let graphRes = null;
        try {
            graphRes = await fetch("/api/workspace/graph");
        } catch(e) {
            console.log("Graph fetch failed", e);
        }

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

        // Render base graph
        if (graphRes.ok) {
            const graphData = await graphRes.json();
            renderBaseGraph(graphData);
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

CRITICAL TOOL USAGE RULES (READ CAREFULLY):
1. When user mentions a specific file name → IMMEDIATELY call read_file with that filename
2. When user asks about patterns, imports, or code structure → IMMEDIATELY call grep_search
3. When user asks "how does X work" and X is a file → IMMEDIATELY call read_file
4. NEVER say "let me check", "I'll look at", "I'll read", or "I'll search" WITHOUT calling the tool IN THE SAME RESPONSE
5. If you mention a file name in your response, you MUST call read_file for that file

BAD EXAMPLE (DON'T DO THIS):
User: "How does parser.py work?"
Bad Response: "Let me check that file for you." ❌ (talking about tool use, not using it)

GOOD EXAMPLE (DO THIS):
User: "How does parser.py work?"
Good Response: [Immediately calls read_file with file_path="parser.py"] ✅

Tools available: grep_search, file_search, list_directory, read_file, get_function_info, mark_as_discussed, write_to_rules

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
                responseModalities: ["AUDIO"], // Function calling works with AUDIO modality
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

        // 2. Model audio output or function calls
        if (sc.modelTurn && sc.modelTurn.parts) {
            for (const part of sc.modelTurn.parts) {
                if (part.inlineData && part.inlineData.data) {
                    state.audioChunksReceived++;
                    if (state.audioChunksReceived % 10 === 1) {
                        const sizeKB = (part.inlineData.data.length * 0.75 / 1024).toFixed(1);
                        debugLog('🔊', 'AUDIO_OUT', `Chunk #${state.audioChunksReceived} (${sizeKB}KB) mimeType=${part.inlineData.mimeType || 'pcm'}`);
                    }
                    if (state.audioPlayer) {
                        state.audioPlayer.play(part.inlineData.data);
                    }
                }
                if (part.functionCall) {
                    // Preemptively enter TOOL_PENDING as soon as we see a functionCall stub
                    // This prevents audio streaming during the gap before the actual toolCall message arrives
                    state.audioState = 'TOOL_PENDING';
                    debugLog('🔧', 'TOOL_PRE', `Detected functionCall stub, entering TOOL_PENDING`);
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
                (state.currentUserMsg._bodyEl || state.currentUserMsg).textContent += text;
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
                (state.currentAssistantMsg._bodyEl || state.currentAssistantMsg).textContent += text;
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
            // Do NOT change audioState here — toolCall may be incoming
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
    state.isUserSpeaking = false;
    
    // Clear dynamic graph nodes (keep base nodes intact)
    clearDynamicGraphNodes();

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

        let myvad = null;
        try {
            // Provide visual feedback while VAD model downloads (~2MB)
            addSystemMessage("Loading VAD model...");
            myvad = await vad.MicVAD.new({
                stream: stream,
                positiveSpeechThreshold: 0.80,  // Slightly lower to catch speech easier
                negativeSpeechThreshold: 0.40,  // Lower to keep speech active longer
                minSpeechFrames: 3,             // Fewer frames to start
                preSpeechPadFrames: 10,         // More padding before speech
                redemptionFrames: 25,           // Much longer grace period (was 12)
                onSpeechStart: () => {
                    console.log("🗣️ VAD: Speech STARTED");
                    state.isUserSpeaking = true;
                },
                onSpeechEnd: (audio) => {
                    console.log("⏹️ VAD: Speech ENDED");
                    state.isUserSpeaking = false;
                },
                onVADMisfire: () => {
                    console.log("🚫 VAD: Misfire (false alarm)");
                    state.isUserSpeaking = false;
                }
            });
            myvad.start();
            addSystemMessage("VAD ready — speak to begin");
        } catch (e) {
            console.error("VAD initialization failed:", e);
            addSystemMessage("VAD failed to load — microphone may stay open.");
            // Fallback (always true) if VAD fails
            state.isUserSpeaking = true;
        }

        workletNode.port.onmessage = (event) => {
            if (event.data.type !== "pcm" || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;

            const { data } = event.data;

            if (state.audioState === 'TOOL_PENDING') {
                return; // Rigid block during tool execution
            }

            // Debug: Log the state to understand why audio isn't flowing
            if (state.audioChunksSent === 0 || state.audioChunksSent % 100 === 1) {
                console.log(`🎤 Worklet state: isUserSpeaking=${state.isUserSpeaking}, audioState=${state.audioState}`);
            }

            if (state.isUserSpeaking) {
                if (state.audioState !== 'STREAMING') {
                    state.audioState = 'STREAMING';
                }
                sendAudioChunk(data);
            } else {
                if (state.audioState === 'STREAMING') {
                    state.audioState = 'IDLE';
                }
            }
        };

        source.connect(workletNode);
        workletNode.connect(audioContext.destination); // Required for worklet to process

        state.audioCapture = {
            stream,
            audioContext,
            source,
            workletNode,
            myvad,
            stop() {
                if (this.myvad) this.myvad.pause();
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

                const toolFilename = fc.args?.path || fc.args?.file_path || fc.args?.filename || fc.args?.file || fc.args?.name || null;
                if (toolFilename && typeof graphState !== 'undefined') {
                    const shortName = toolFilename.split('/').pop().split('\\').pop();
                    
                    let nodeFile = toolFilename;
                    
                    const existingNodeKey = Object.keys(graphState.nodes).find(k => k.endsWith(shortName));
                    if(existingNodeKey){
                        nodeFile = existingNodeKey;
                    } else {
                        nodeFile = shortName;
                    }

                    const prevActive = Object.keys(graphState.nodes).find(k => graphState.nodes[k].state === 'active');
                    
                    if(existingNodeKey) {
                        setActiveNode(nodeFile);
                        const node = graphState.nodes[nodeFile];
                        if (node) {
                            node.touchCount = (node.touchCount || 0) + 1;
                            node.el.setAttribute('data-touches', Math.min(node.touchCount, 10));
                        }
                    } else {
                        addGraphNode(nodeFile, "visited dynamic", nodeFile, 1.0, false, 0);
                        setActiveNode(nodeFile);
                        const node = graphState.nodes[nodeFile];
                        if (node) {
                            node.touchCount = 1;
                            node.el.setAttribute('data-touches', '1');
                        }
                    }

                    if (state.currentAssistantMsg) {
                        state.currentAssistantMsg.setAttribute('data-file', nodeFile);
                    }

                    if (prevActive && prevActive !== nodeFile) {
                        addGraphEdge(prevActive, nodeFile, true, true);
                    }
                    
                    if (typeof updateGraphSimulation === "function") {
                        updateGraphSimulation();
                    }
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

    if (type === "system") {
        // Minimal system pill — no role row
        const body = document.createElement("div");
        body.className = "message-body";
        body.textContent = text;
        div._bodyEl = body;
        div.appendChild(body);
    } else {
        // Role header row
        const roleRow = document.createElement("div");
        roleRow.className = "message-role";
        roleRow.setAttribute("aria-hidden", "true");

        const roleDot = document.createElement("span");
        roleDot.className = "message-role-dot";

        const roleLabel = document.createElement("span");
        roleLabel.className = "message-role-label";
        roleLabel.textContent = type === "user" ? "You" : "Codiey";

        roleRow.appendChild(roleDot);
        roleRow.appendChild(roleLabel);

        // Streaming body
        const body = document.createElement("div");
        body.className = "message-body";
        body.textContent = text;
        div._bodyEl = body;

        div.appendChild(roleRow);
        div.appendChild(body);
    }

    div.addEventListener('click', (e) => {
        const file = div.getAttribute('data-file');
        if (file) {
            const rect = div.getBoundingClientRect();
            if (e.clientX - rect.left < 24) {
                highlightGraphNode(file);
            }
        }
    });

    container.appendChild(div);
    scrollTranscript();
    return div;
}

function highlightGraphNode(filename) {
    const node = graphState.nodes[filename];
    if (node && node.el) {
        node.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        
        node.el.style.animation = 'none';
        setTimeout(() => {
            node.el.style.animation = 'nodeAppear 0.6s ease-out';
        }, 10);
        
        if (graphTooltip) {
            const rect = node.el.getBoundingClientRect();
            const touches = node.touchCount || 0;
            const touchText = touches > 0 ? ` • ${touches} discussion${touches !== 1 ? 's' : ''}` : '';
            graphTooltip.innerHTML = `${node.name} <span class="pagerank-score">PageRank: ${(node.score || 0).toFixed(3)}${touchText}</span>`;
            graphTooltip.style.left = `${rect.left + 24}px`;
            graphTooltip.style.top = `${rect.top - 12}px`;
            graphTooltip.classList.add("visible");
            
            setTimeout(() => {
                if (graphTooltip) graphTooltip.classList.remove("visible");
            }, 3000);
        }
    }
}

function addSystemMessage(text) {
    addMessage(`[${text}]`, "system");
}

function addToolMessage(text) {
    const container = document.getElementById("messages-container");
    const div = document.createElement("div");
    div.className = "message tool";
    div.innerHTML = `<span class="tool-call-icon">⌕</span><span class="tool-call-name">${text}</span>`;
    container.appendChild(div);
    scrollTranscript();
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
    nodes: {},  // filename -> D3 node data
    links: [],  // array of D3 link data
    nodeCount: 0,
    simulation: null
};

let graphTooltip = null;
let graphZoom = null;

function initGraphTooltip() {
    if (!graphTooltip) {
        graphTooltip = document.createElement("div");
        graphTooltip.className = "graph-tooltip";
        document.body.appendChild(graphTooltip);
    }
}

function clearDynamicGraphNodes() {
    const svg = document.getElementById("graph-svg");
    
    // remove dynamic nodes from DOM and state
    for(const k of Object.keys(graphState.nodes)) {
        if(!graphState.nodes[k].isBase) {
            graphState.nodes[k].el.remove();
            delete graphState.nodes[k];
            graphState.nodeCount--;
        } else {
            // reset base nodes
            graphState.nodes[k].el.className = "graph-node base";
            graphState.nodes[k].state = "base";
        }
    }
    
    // remove dynamic edges
    graphState.links = graphState.links.filter(l => l.isBase);

    if(svg) {
        const dynamicEdges = svg.querySelectorAll('.graph-edge.dynamic, .graph-edge.traversing');
        dynamicEdges.forEach(e => e.remove());
    }

    if (graphState.simulation) updateGraphSimulation();
    
    // Update badge
    const badge = document.getElementById("node-count-badge");
    if(badge) {
        badge.textContent = `${graphState.nodeCount} node${graphState.nodeCount !== 1 ? 's' : ''}`;
    }
}

function renderBaseGraph(graphData) {
    initGraphTooltip();

    const container = document.getElementById("graph-nodes");
    if (!container) return;
    
    container.innerHTML = ""; // clear all nodes
    const svg = document.getElementById("graph-svg");
    if (svg) {
        // clear all edges except defs
        const defs = svg.querySelector('defs');
        svg.innerHTML = '';
        if(defs) svg.appendChild(defs);
    }

    graphState.nodes = {};
    graphState.links = [];
    graphState.nodeCount = 0;

    const nodes = graphData.nodes || [];
    const edges = graphData.edges || [];

    nodes.forEach((node) => {
        const shortName = node.id.split('/').pop().split('\\').pop();
        
        const scale = 0.7 + (node.score * 2.5);
        
        addGraphNode(node.id, "base", shortName, scale, true, node.score);
    });

    edges.forEach(edge => {
        addGraphEdge(edge.source, edge.target, false);
    });

    setupD3Simulation();
}

function setupD3Simulation() {
    const container = document.getElementById("graph-canvas");
    const width = container.clientWidth || 800;
    const height = container.clientHeight || 600;

    if (graphState.simulation) {
        graphState.simulation.stop();
    }

    graphState.simulation = d3.forceSimulation()
        .force("link", d3.forceLink().id(d => d.id).distance(140))
        .force("charge", d3.forceManyBody().strength(-500))
        .force("x", d3.forceX(width / 2).strength(0.04))
        .force("y", d3.forceY(height / 2).strength(0.04))
        .force("collide", d3.forceCollide().radius(60))
        .on("tick", onSimulationTick);

    // Init zoom/pan behavior
    graphZoom = d3.zoom()
        .scaleExtent([0.2, 4])
        .filter((event) => {
            // Allow wheel zoom and pointer-based pan
            return !event.button || event.type === 'wheel';
        })
        .on("zoom", (event) => {
            const inner = document.getElementById("graph-inner");
            if (inner) {
                const {x, y, k} = event.transform;
                inner.style.transform = `translate(${x}px, ${y}px) scale(${k})`;
            }
        });
    
    d3.select("#graph-canvas")
        .call(graphZoom)
        .on("dblclick.zoom", null); // disable double-click zoom

    updateGraphSimulation();
}

function updateGraphSimulation() {
    if (!graphState.simulation) return;
    const nodesArr = Object.values(graphState.nodes);
    graphState.simulation.nodes(nodesArr);
    graphState.simulation.force("link").links(graphState.links);
    graphState.simulation.alpha(1).restart();
}

function onSimulationTick() {
    // Update node positions (no clamping - free pan/zoom)
    Object.values(graphState.nodes).forEach(d => {
        d.el.style.left = `${d.x}px`;
        d.el.style.top = `${d.y}px`;
    });

    // Update SVG links
    graphState.links.forEach(l => {
        if(l.lineEl && l.source.x !== undefined && l.target.x !== undefined) {
            l.lineEl.setAttribute("x1", l.source.x);
            l.lineEl.setAttribute("y1", l.source.y);
            l.lineEl.setAttribute("x2", l.target.x);
            l.lineEl.setAttribute("y2", l.target.y);
        }
    });
}

function addGraphNode(filename, nodeState, displayName = null, scale = 1.0, isBase = false, score = 0) {
    if (graphState.nodes[filename]) return graphState.nodes[filename];

    const container = document.getElementById("graph-nodes");
    const el = document.createElement("div");
    el.className = `graph-node ${nodeState}`;
    el.dataset.filename = filename;
    el.style.transform = `translate(-50%, -50%)`;
    el.style.animation = 'nodeAppear 0.6s ease-out both';

    const display = displayName || filename.split('/').pop().split('\\').pop();
    const ext = display.split('.').pop().toLowerCase();
    
    // Pill-style node: ext dot + label
    el.innerHTML = `<span class="node-ext" data-ext="${ext}"></span><span class="node-label">${display}</span>`;
    
    el.addEventListener('mouseenter', () => {
        if (!graphTooltip) return;
        const rect = el.getBoundingClientRect();
        const touches = nodeData.touchCount || 0;
        const touchText = touches > 0 ? ` • ${touches} discussion${touches !== 1 ? 's' : ''}` : '';
        graphTooltip.innerHTML = `${display} <span class="pagerank-score">PageRank: ${(score || 0).toFixed(3)}${touchText}</span>`;
        graphTooltip.style.left = `${rect.left + 24}px`;
        graphTooltip.style.top = `${rect.top - 12}px`;
        graphTooltip.classList.add("visible");
        
        const messagesWithFile = document.querySelectorAll(`[data-file="${filename}"]`);
        messagesWithFile.forEach(msg => msg.classList.add('highlight'));
    });
    
    el.addEventListener('mouseleave', () => {
        if (graphTooltip) graphTooltip.classList.remove("visible");
        
        const messagesWithFile = document.querySelectorAll(`[data-file="${filename}"]`);
        messagesWithFile.forEach(msg => msg.classList.remove('highlight'));
    });
    
    el.addEventListener('click', () => {
        showNodeDetailPanel(filename, display, score, nodeData.touchCount || 0);
    });
    
    container.appendChild(el);

    const container2 = document.getElementById("graph-canvas");
    const width = container2 ? container2.clientWidth : 800;
    const height = container2 ? container2.clientHeight : 600;

    const nodeData = {
        id: filename,
        name: display,
        el,
        state: nodeState,
        isBase,
        scale: 1.0,
        score,
        touchCount: 0,
        x: width / 2 + (Math.random() - 0.5) * 200,
        y: height / 2 + (Math.random() - 0.5) * 200
    };

    graphState.nodes[filename] = nodeData;
    graphState.nodeCount++;

    const badge = document.getElementById("node-count-badge");
    if(badge) badge.textContent = `${graphState.nodeCount} node${graphState.nodeCount !== 1 ? 's' : ''}`;

    return nodeData;
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
        entry.el.className = `graph-node active ${entry.isBase ? 'base' : 'dynamic'}`;
        entry.state = "active";
        
        // Auto-pan camera to center active node
        if (graphZoom && entry.x !== undefined && entry.y !== undefined) {
            const canvas = document.getElementById("graph-canvas");
            if (canvas) {
                const cx = canvas.clientWidth / 2;
                const cy = canvas.clientHeight / 2;
                const t = d3.zoomIdentity.translate(cx - entry.x, cy - entry.y);
                d3.select("#graph-canvas").transition().duration(450).call(graphZoom.transform, t);
            }
        }
    }

    const bar = document.getElementById("breadcrumb-content");
    if (!bar) return;
    const parts = filename.replace(/[()]/g, '').split(/[\/\\\.]/);
    const segments = parts.map((p, i) => {
        if (i === parts.length - 1) {
            return `<span class="breadcrumb-active">${filename}</span>`;
        }
        return `<span class="breadcrumb-segment">${p}</span><span class="breadcrumb-sep">›</span>`;
    });
    bar.innerHTML = segments.join('');
}

function addGraphEdge(fromFile, toFile, isDynamic = false, isTraversal = false) {
    const from = graphState.nodes[fromFile];
    const to = graphState.nodes[toFile];
    if (!from || !to) return;

    const existing = graphState.links.find(l => 
        (l.source === from || l.source.id === fromFile) && 
        (l.target === to || l.target.id === toFile)
    );

    if (existing) {
        if (isTraversal && existing.lineEl) {
            existing.lineEl.classList.remove('base', 'dynamic');
            void existing.lineEl.offsetWidth; 
            existing.lineEl.classList.add('traversing');
            
            animateTraversalParticle(from, to, existing.lineEl);
        }
        return;
    }

    const svg = document.getElementById("graph-svg");
    if(!svg) return;
    const ns = "http://www.w3.org/2000/svg";
    const line = document.createElementNS(ns, "line");
    line.setAttribute("class", `graph-edge ${isTraversal ? 'traversing' : (isDynamic ? 'dynamic' : 'base')}`);
    if (isDynamic || isTraversal) {
        line.setAttribute("marker-end", "url(#arrow)");
    }
    svg.appendChild(line);

    const linkData = {
        source: fromFile,
        target: toFile,
        lineEl: line,
        isBase: !isDynamic && !isTraversal
    };

    graphState.links.push(linkData);
    
    if (isTraversal) {
        setTimeout(() => animateTraversalParticle(from, to, line), 100);
    }
}

function animateTraversalParticle(from, to, lineEl) {
    if (!from || !to || !from.x || !to.x) return;
    
    const svg = document.getElementById("graph-svg");
    if (!svg) return;
    
    const ns = "http://www.w3.org/2000/svg";
    const particle = document.createElementNS(ns, "circle");
    particle.setAttribute("class", "traversal-particle");
    particle.setAttribute("r", "4");
    particle.setAttribute("cx", from.x);
    particle.setAttribute("cy", from.y);
    svg.appendChild(particle);
    
    const duration = 800;
    const startTime = performance.now();
    
    function animate(currentTime) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        
        const x = from.x + (to.x - from.x) * progress;
        const y = from.y + (to.y - from.y) * progress;
        
        particle.setAttribute("cx", x);
        particle.setAttribute("cy", y);
        
        if (progress < 1) {
            requestAnimationFrame(animate);
        } else {
            particle.remove();
        }
    }
    
    requestAnimationFrame(animate);
}

function addRule(text) {
    const list = document.getElementById("rules-list");
    const item = document.createElement("div");
    item.className = "rule-item flash";
    item.innerHTML = `<span class="rule-dot"></span><span class="rule-text">${text}</span>`;
    list.prepend(item);
    setTimeout(() => item.classList.remove("flash"), 2000);
    
    updateInsightsCount();
}

function updateInsightsCount() {
    const list = document.getElementById("rules-list");
    const count = list ? list.children.length : 0;
    const countEl = document.getElementById("insights-count");
    if (countEl) {
        countEl.textContent = `${count} insight${count !== 1 ? 's' : ''}`;
    }
}

function initializeInsightsOverlay() {
    const toggle = document.getElementById("insights-toggle");
    const overlay = document.getElementById("insights-overlay");
    const panel = document.getElementById("insights-panel");
    
    if (toggle && overlay && panel) {
        toggle.addEventListener('click', () => {
            overlay.classList.toggle('insights-collapsed');
            panel.classList.toggle('hidden');
        });
    }
    
    updateInsightsCount();
}

document.addEventListener('DOMContentLoaded', () => {
    initializeInsightsOverlay();
});

function setSessionState(sessionState) {
    const dot = document.getElementById("status-dot");
    const text = document.getElementById("status-text");
    const orb = document.getElementById("orb");
    const waveform = document.getElementById("waveform");
    const stateLabel = document.getElementById("voice-state-label");

    orb.className = "orb-container";
    if (stateLabel) stateLabel.className = "";

    switch (sessionState) {
        case "listening":
            dot.className = "status-dot status-connected";
            text.textContent = "LISTENING";
            orb.classList.add("listening");
            waveform.classList.remove("active");
            if (stateLabel) { stateLabel.textContent = "Listening\u2026"; stateLabel.classList.add("state-listening"); }
            break;
        case "speaking":
            dot.className = "status-dot status-connected";
            text.textContent = "CODIEY SPEAKING";
            orb.classList.add("speaking");
            waveform.classList.add("active");
            if (stateLabel) { stateLabel.textContent = "Speaking"; stateLabel.classList.add("state-speaking"); }
            break;
        case "analyzing":
            dot.className = "status-dot status-connected";
            text.textContent = "ANALYZING";
            orb.classList.add("analyzing");
            waveform.classList.remove("active");
            if (stateLabel) { stateLabel.textContent = "Analyzing\u2026"; stateLabel.classList.add("state-analyzing"); }
            break;
    }
}

function showNodeDetailPanel(filename, displayName, score, touchCount) {
    const panel = document.getElementById("code-panel");
    const fileNameEl = document.getElementById("code-filename");
    const bodyEl = document.getElementById("code-body");
    
    panel.classList.remove("hidden");
    fileNameEl.textContent = displayName;
    
    const importance = score > 0.15 ? "Core file" : score > 0.08 ? "Important file" : score > 0.03 ? "Supporting file" : "Leaf node";
    const connections = Object.values(graphState.links).filter(l => 
        (l.source === filename || l.source.id === filename || l.target === filename || l.target.id === filename)
    ).length;
    
    bodyEl.innerHTML = `
        <div style="padding: 20px; font-family: var(--font-mono); font-size: 12px; line-height: 1.8; color: var(--text-secondary);">
            <div style="margin-bottom: 20px;">
                <div style="color: var(--text-muted); font-size: 10px; text-transform: uppercase; margin-bottom: 8px;">File Path</div>
                <div style="color: var(--text-primary);">${filename}</div>
            </div>
            
            <div style="margin-bottom: 20px;">
                <div style="color: var(--text-muted); font-size: 10px; text-transform: uppercase; margin-bottom: 8px;">PageRank Score</div>
                <div style="color: var(--teal); font-size: 16px; font-weight: 600;">${score.toFixed(4)}</div>
                <div style="color: var(--text-secondary); font-size: 11px; margin-top: 4px;">${importance}</div>
            </div>
            
            <div style="margin-bottom: 20px;">
                <div style="color: var(--text-muted); font-size: 10px; text-transform: uppercase; margin-bottom: 8px;">Graph Metrics</div>
                <div>${connections} connection${connections !== 1 ? 's' : ''}</div>
                <div>${touchCount} discussion${touchCount !== 1 ? 's' : ''} this session</div>
            </div>
            
            <button onclick="scrollToFileInConversation('${filename}')" style="
                background: var(--teal-15);
                color: var(--teal);
                border: 1px solid var(--teal-25);
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-family: var(--font-mono);
                font-size: 11px;
                margin-top: 12px;
            ">Show in conversation →</button>
            
            <button onclick="document.getElementById('code-panel').classList.add('hidden')" style="
                background: transparent;
                color: var(--text-muted);
                border: 1px solid var(--border);
                padding: 8px 16px;
                border-radius: 6px;
                cursor: pointer;
                font-family: var(--font-mono);
                font-size: 11px;
                margin-top: 8px;
                margin-left: 8px;
            ">Close</button>
        </div>
    `;
}

function scrollToFileInConversation(filename) {
    const messages = document.querySelectorAll(`[data-file="${filename}"]`);
    if (messages.length > 0) {
        messages[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
        messages.forEach(msg => {
            msg.classList.add('highlight');
            setTimeout(() => msg.classList.remove('highlight'), 2000);
        });
    }
}

console.log("🎙️ Codiey loaded");
