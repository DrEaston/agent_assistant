/* Dieter's animation player (docs/mascot_plan.md). Clips come from /media/lab/clips.json and their sprite sheets
   (/media/lab/sheet/<name>/<page>.webp, `per_page` frames each): every frame is a CANVAS-square tile, `columns`
   across, all lined up the same way.
   DieterMascot.load() fetches them; a Viewer draws one clip or a join between two, at any speed, a frame at a time. */
(function () {
  const sheets = {};

  async function load(base = "/media/lab") {
    const lab = await (await fetch(base + "/clips.json", { cache: "no-store" })).json();
    const jobs = [];
    for (const c of lab.clips) {
      sheets[c.name] = [];
      for (let p = 0; p < Math.ceil(c.frames / c.per_page); p++) {
        jobs.push(new Promise((resolve) => {
          const img = new Image();
          img.onload = () => { sheets[c.name][p] = img; resolve(); };
          img.onerror = () => resolve();
          img.src = `${base}/sheet/${c.name}/${p}.webp`;
        }));
      }
    }
    await Promise.all(jobs);
    lab.byName = Object.fromEntries(lab.clips.map((c) => [c.name, c]));
    return lab;
  }

  function draw(ctx, lab, name, frame, alpha = 1) {
    const clip = lab.byName[name], size = lab.canvas;
    if (!clip) return;
    const f = Math.max(0, Math.min(clip.frames - 1, frame));
    const img = (sheets[name] || [])[Math.floor(f / clip.per_page)], n = f % clip.per_page;
    if (!img) return;
    ctx.globalAlpha = alpha;
    ctx.drawImage(img, (n % clip.columns) * size, Math.floor(n / clip.columns) * size, size, size,
                  0, 0, ctx.canvas.width, ctx.canvas.height);
    ctx.globalAlpha = 1;
  }

  // A sequence is a list of [clip name, frame] steps. A clip plays its frames (pingpong: there and back);
  // a join is the last part of one clip followed by the first part of the next.
  function framesOf(clip) {
    const all = [...Array(clip.frames).keys()];
    return clip.play === "pingpong" ? all.concat(all.slice(1, -1).reverse()) : all;
  }
  function sequence(lab, what) {
    if (what.startsWith("join:")) {
      const [a, b] = what.slice(5).split(">").map((n) => lab.byName[n]);
      const tail = framesOf(a).slice(-8).map((f) => [a.name, f]);
      return tail.concat(framesOf(b).slice(0, 8).map((f) => [b.name, f]));
    }
    const clip = lab.byName[what];
    return framesOf(clip).map((f) => [clip.name, f]);
  }

  class Viewer {
    constructor(canvas, lab, onFrame) {
      this.ctx = canvas.getContext("2d"); this.lab = lab; this.onFrame = onFrame || (() => {});
      this.speed = 1; this.onion = false; this.playing = false; this.at = 0; this.steps = []; this.what = "";
    }
    show(what) { this.what = what; this.steps = sequence(this.lab, what); this.at = 0; this.render(); }
    render() {
      const ctx = this.ctx, step = this.steps[this.at];
      ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
      if (!step) return;
      if (this.onion) {  // the frame before, faint, under this one: drift shows as a double outline
        const prev = this.steps[(this.at - 1 + this.steps.length) % this.steps.length];
        draw(ctx, this.lab, prev[0], prev[1], 0.45);
        draw(ctx, this.lab, step[0], step[1], 0.6);
      } else draw(ctx, this.lab, step[0], step[1]);
      this.onFrame(step, this.at, this.steps.length);
    }
    step(by) { this.at = (this.at + by + this.steps.length) % this.steps.length; this.render(); }
    play() {
      if (this.playing) return; this.playing = true;
      const tick = () => {
        if (!this.playing) return;
        const step = this.steps[this.at], clip = this.lab.byName[step[0]];
        this.step(1);
        this.timer = setTimeout(tick, (clip ? clip.ms : 100) / this.speed);
      };
      tick();
    }
    pause() { this.playing = false; clearTimeout(this.timer); }
  }

  window.DieterMascot = { load, draw, sequence, Viewer };
})();
