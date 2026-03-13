/**
 * PCM Audio Processor — AudioWorklet for mic capture.
 * Captures microphone input and converts to 16-bit PCM at 16kHz.
 * Adapted from Google's gemini-live-api-examples.
 */
class PCMProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.bufferSize = 2048;
        this.buffer = new Float32Array(this.bufferSize);
        this.bufferIndex = 0;
    }

    process(inputs, outputs, parameters) {
        const input = inputs[0];
        if (!input || !input[0]) return true;

        const inputChannel = input[0];

        for (let i = 0; i < inputChannel.length; i++) {
            this.buffer[this.bufferIndex++] = inputChannel[i];

            if (this.bufferIndex >= this.bufferSize) {
                // Compute RMS energy from the raw Float32 samples
                let sumSquares = 0;
                for (let j = 0; j < this.bufferSize; j++) {
                    sumSquares += this.buffer[j] * this.buffer[j];
                }
                const energy = Math.sqrt(sumSquares / this.bufferSize);

                // Convert Float32 to Int16 PCM
                const pcmData = new Int16Array(this.bufferSize);
                for (let j = 0; j < this.bufferSize; j++) {
                    const sample = Math.max(-1, Math.min(1, this.buffer[j]));
                    pcmData[j] = sample < 0
                        ? sample * 0x8000
                        : sample * 0x7FFF;
                }

                // Send PCM buffer to main thread
                this.port.postMessage({
                    type: 'pcm',
                    data: pcmData.buffer,
                    energy: energy
                }, [pcmData.buffer]);

                this.buffer = new Float32Array(this.bufferSize);
                this.bufferIndex = 0;
            }
        }

        return true;
    }
}

registerProcessor('pcm-processor', PCMProcessor);
