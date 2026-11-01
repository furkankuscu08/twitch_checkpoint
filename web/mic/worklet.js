import {createResampler} from './resampler.js';

class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.convert = createResampler(sampleRate);
    this.generation = 0;
    this.pending = 0;
    this.maxPending = options.processorOptions.maxPendingFrames;
    this.gap = false;
    this.port.onmessage = ({data}) => {
      if (data.type === 'reset') {
        this.convert = createResampler(sampleRate);
        this.generation = data.generation;
        this.pending = 0;
        this.gap = false;
      } else if (data.type === 'ack' && data.generation === this.generation) {
        this.pending = Math.max(0, this.pending - 1);
      }
    };
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input) {
      for (const pcm of this.convert(input)) {
        // Bound the port queue to the engine's PCM buffer duration.
        if (this.pending >= this.maxPending) { this.gap = true; continue; }
        this.pending += 1;
        let energy = 0;
        for (const sample of pcm) energy += (sample / 32768) ** 2;
        this.port.postMessage({pcm: pcm.buffer, level: Math.sqrt(energy / pcm.length),
          generation: this.generation, gap: this.gap}, [pcm.buffer]);
        this.gap = false;
      }
    }
    return true;
  }
}
registerProcessor('minefield-mic', MicProcessor);
