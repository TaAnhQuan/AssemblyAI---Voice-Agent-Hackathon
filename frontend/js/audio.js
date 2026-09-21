/**
 * audio.js — Encapsulates Web Audio API, PCM 24kHz capture,
 * gapless buffer scheduling, and interruption flushing.
 */
class VoiceAudioEngine {
  constructor(sampleRate = 24000) {
    this.sampleRate = sampleRate;
    this.audioCtx = null;
    this.micStream = null;
    this.processorNode = null;
    this.analyserNode = null;
    this.activeSources = [];
    this.nextStartTime = 0;
    this.isAgentSpeaking = false;
  }

  async initialize() {
    if (!this.audioCtx || this.audioCtx.state === "closed") {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: this.sampleRate,
      });
      await this.audioCtx.resume();
    }

    if (!this.analyserNode) {
      this.analyserNode = this.audioCtx.createAnalyser();
      this.analyserNode.fftSize = 256;
    }
  }

  /**
   * Detects the microphones attached to the system. Device labels are only
   * populated by the browser once permission has been granted at least once,
   * so this briefly opens and immediately closes a mic stream when needed.
   * Throws Error("NO_MICROPHONE") if the system has no audio input device.
   */
  async listInputDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) {
      throw new Error("UNSUPPORTED");
    }

    let devices = await navigator.mediaDevices.enumerateDevices();
    let inputs = devices.filter((d) => d.kind === "audioinput");

    if (inputs.length === 0) {
      throw new Error("NO_MICROPHONE");
    }

    if (inputs.every((d) => !d.label)) {
      const probeStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      probeStream.getTracks().forEach((t) => t.stop());

      devices = await navigator.mediaDevices.enumerateDevices();
      inputs = devices.filter((d) => d.kind === "audioinput");
    }

    return inputs.map((d, i) => ({
      deviceId: d.deviceId,
      label: d.label || `Microphone ${i + 1}`,
    }));
  }

  async startMicrophone(onAudioChunk, onRmsUpdate, deviceId = null) {
    await this.initialize();

    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        channelCount: 1,
        sampleRate: this.sampleRate,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    const micSource = this.audioCtx.createMediaStreamSource(this.micStream);
    micSource.connect(this.analyserNode);

    // 2048 samples @ 24kHz = ~85ms frames
    this.processorNode = this.audioCtx.createScriptProcessor(2048, 1, 1);

    this.processorNode.onaudioprocess = (e) => {
      const inputChannel = e.inputBuffer.getChannelData(0);
      const pcm16 = new Int16Array(inputChannel.length);
      let sumSquares = 0;

      for (let i = 0; i < inputChannel.length; i++) {
        const sample = Math.max(-1, Math.min(1, inputChannel[i]));
        pcm16[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
        sumSquares += sample * sample;
      }

      if (onAudioChunk) {
        onAudioChunk(pcm16.buffer);
      }

      if (onRmsUpdate) {
        const rms = Math.sqrt(sumSquares / inputChannel.length) * 1000;
        onRmsUpdate(rms);
      }
    };

    micSource.connect(this.processorNode);
    this.processorNode.connect(this.audioCtx.destination);
  }

  playPcmChunk(arrayBuffer, onPlaybackEnded) {
    this.isAgentSpeaking = true;

    const pcm16 = new Int16Array(arrayBuffer);
    const float32 = new Float32Array(pcm16.length);

    for (let i = 0; i < pcm16.length; i++) {
      float32[i] = pcm16[i] / 32768.0;
    }

    const audioBuffer = this.audioCtx.createBuffer(1, float32.length, this.sampleRate);
    audioBuffer.getChannelData(0).set(float32);

    const source = this.audioCtx.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.analyserNode);
    this.analyserNode.connect(this.audioCtx.destination);

    const currentTime = this.audioCtx.currentTime;
    if (this.nextStartTime < currentTime) {
      this.nextStartTime = currentTime;
    }

    source.start(this.nextStartTime);
    this.nextStartTime += audioBuffer.duration;

    this.activeSources.push(source);
    source.onended = () => {
      this.activeSources = this.activeSources.filter((s) => s !== source);
      if (this.activeSources.length === 0) {
        this.isAgentSpeaking = false;
        if (onPlaybackEnded) onPlaybackEnded();
      }
    };
  }

  flush() {
    this.activeSources.forEach((source) => {
      try {
        source.stop(0);
      } catch (e) {}
    });
    this.activeSources = [];
    this.isAgentSpeaking = false;
    if (this.audioCtx) {
      this.nextStartTime = this.audioCtx.currentTime;
    }
  }

  stop() {
    this.flush();
    if (this.processorNode) {
      this.processorNode.disconnect();
      this.processorNode = null;
    }
    if (this.micStream) {
      this.micStream.getTracks().forEach((track) => track.stop());
      this.micStream = null;
    }
    if (this.audioCtx && this.audioCtx.state !== "closed") {
      this.audioCtx.close();
      this.audioCtx = null;
    }
    this.analyserNode = null;
    this.nextStartTime = 0;
  }

  startSpectrumVisualizer(canvasElement, isConnectedGetter) {
    if (!canvasElement || !this.analyserNode) return;
    const ctx = canvasElement.getContext("2d");
    const bufferLength = this.analyserNode.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    const draw = () => {
      if (!isConnectedGetter()) {
        ctx.clearRect(0, 0, canvasElement.width, canvasElement.height);
        return;
      }
      requestAnimationFrame(draw);

      this.analyserNode.getByteFrequencyData(dataArray);
      ctx.clearRect(0, 0, canvasElement.width, canvasElement.height);

      const barWidth = canvasElement.width / 24;
      let x = 0;

      for (let i = 0; i < 24; i++) {
        const barHeight = (dataArray[i * 4] / 255) * canvasElement.height;
        ctx.fillStyle = this.isAgentSpeaking ? "#3525cd" : "#006c4a";
        ctx.fillRect(x, canvasElement.height - barHeight, barWidth - 1, barHeight);
        x += barWidth;
      }
    };
    draw();
  }
}

window.VoiceEngine = new VoiceAudioEngine(16000);