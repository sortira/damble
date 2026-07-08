/* Client-side emotion detection using face-api.js (vladmandic fork).
 *
 * Each player's own browser measures their laughter from the webcam and reports
 * it to the server. Nothing leaves the browser except two numbers per round.
 *
 * Laughter is bursty (dead time while reading the joke, a burst, then a fade),
 * so a plain mean over the whole window collapses toward zero. Instead we score
 * the TOP-K average: keep the highest `TOP_FRACTION` of frames and average them,
 * roughly "your best few seconds of laughing." This ignores the ramp-up and
 * fade, resists single fluke frames (top-K is many frames, so one spike is
 * diluted), and still returns 0 if you hide the whole time (every frame is 0).
 */
const Emotion = (() => {
  const MODEL_URL = "https://cdn.jsdelivr.net/npm/@vladmandic/face-api/model/";
  const SAMPLE_INTERVAL_MS = 120;
  const TOP_FRACTION = 0.4; // score = mean of the best 40% of frames
  let loaded = false;
  let videoEl = null;
  let stream = null;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function loadModels() {
    if (loaded) return;
    if (typeof faceapi === "undefined") {
      throw new Error("face-api failed to load (check network / CDN)");
    }
    await faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL);
    await faceapi.nets.faceExpressionNet.loadFromUri(MODEL_URL);
    loaded = true;
  }

  async function startCamera(el) {
    videoEl = el;
    if (!stream) {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { width: 320, height: 240 },
        audio: false,
      });
    }
    videoEl.srcObject = stream;
    try { await videoEl.play(); } catch (_) { /* autoplay quirks */ }
    return stream;
  }

  // Point another <video> at the already-open stream (lobby -> game view).
  function attach(el) {
    if (!stream) return;
    videoEl = el;
    el.srcObject = stream;
    el.play().catch(() => {});
  }

  // One reading of laugh intensity in [0,1], or null if no face is found.
  // Big open-mouth laughs partly register as "surprised", so we blend a little.
  async function detectHappy() {
    if (!loaded || !videoEl || videoEl.readyState < 2) return null;
    const det = await faceapi
      .detectSingleFace(
        videoEl,
        new faceapi.TinyFaceDetectorOptions({ inputSize: 224, scoreThreshold: 0.4 })
      )
      .withFaceExpressions();
    if (!det) return null;
    const e = det.expressions;
    return Math.min(1, e.happy + e.surprised * 0.25);
  }

  // Mean of the highest `frac` share of values (undetected frames are 0s in
  // the pool, so hiding still scores 0). Rewards a sustained burst, not a spike.
  function topKMean(values, frac) {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => b - a);
    const k = Math.max(1, Math.round(sorted.length * frac));
    let sum = 0;
    for (let i = 0; i < k; i++) sum += sorted[i];
    return sum / k;
  }

  // Sample across `windowMs` and return { score, mean, peak }.
  //   score = top-K average (what the game uses)
  //   mean  = plain average (kept for transparency)
  //   peak  = single best frame (shown for feedback)
  async function captureReaction(windowMs, onSample) {
    const end = performance.now() + windowMs;
    const values = [];
    let peak = 0, detected = 0;
    while (performance.now() < end) {
      const h = await detectHappy();
      const v = h == null ? 0 : h;
      values.push(v);
      if (h != null) { detected++; if (h > peak) peak = h; }
      if (onSample) onSample(v);
      await sleep(SAMPLE_INTERVAL_MS);
    }
    const mean = values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;
    const score = topKMean(values, TOP_FRACTION);
    return { score, mean, peak, slots: values.length, detected };
  }

  return {
    loadModels,
    startCamera,
    attach,
    detectHappy,
    captureReaction,
    topKMean,
    isLoaded: () => loaded,
    hasCamera: () => !!stream,
  };
})();
