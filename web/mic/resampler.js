// Streaming area-average resampling. State survives AudioWorklet render blocks.
export function createResampler(inputRate) {
  const ratio = inputRate / 16000;
  let sum = 0, weight = 0, cursor = 0;
  let frame = new Int16Array(320);
  return function resample(input) {
    const frames = [];
    for (const sample of input) {
      let remaining = 1;
      while (remaining > 1e-8) {
        const amount = Math.min(remaining, ratio - weight);
        sum += sample * amount;
        weight += amount;
        remaining -= amount;
        if (weight >= ratio - 1e-8) {
          frame[cursor++] = Math.max(-32768, Math.min(32767, Math.round(sum / ratio * 32768)));
          sum = 0; weight = 0;
          if (cursor === frame.length) {
            frames.push(frame);
            frame = new Int16Array(320);
            cursor = 0;
          }
        }
      }
    }
    return frames;
  };
}
