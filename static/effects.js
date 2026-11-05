/* Local canvas scenery. One bounded canvas and one throttled loop per page. */
(() => {
  "use strict";
  const effectNames = new Set(["matrix", "hearth", "aurora", "starfield", "aquarium", "lava", "rain"]);
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let canvas = null;
  let context = null;
  let config = null;
  let frame = 0;
  let lastFrame = 0;
  let elapsed = 18;
  let width = 1;
  let height = 1;
  let scene = [];
  let paper = "#050805";
  let random = Math.random;
  const TAU = Math.PI * 2;
  const glyphs = "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン0123456789";

  function seedFor(value) {
    let seed = 2166136261;
    for (const char of value) seed = Math.imul(seed ^ char.charCodeAt(0), 16777619);
    return () => {
      seed |= 0;
      seed = (seed + 0x6d2b79f5) | 0;
      let result = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      result = (result + Math.imul(result ^ (result >>> 7), 61 | result)) ^ result;
      return ((result ^ (result >>> 14)) >>> 0) / 4294967296;
    };
  }

  const rgba = (rgb, alpha) => `rgba(${rgb},${Math.max(0, Math.min(1, alpha))})`;
  const dark = () => config.mode === "dark";

  function background(top, bottom) {
    context.fillStyle = paper;
    context.fillRect(0, 0, width, height);
    if (!top) return;
    const gradient = context.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, top);
    gradient.addColorStop(1, bottom);
    context.fillStyle = gradient;
    context.fillRect(0, 0, width, height);
  }

  function glow(x, y, radius, rgb, opacity) {
    const gradient = context.createRadialGradient(x, y, 0, x, y, Math.max(1, radius));
    gradient.addColorStop(0, rgba(rgb, opacity));
    gradient.addColorStop(1, rgba(rgb, 0));
    context.fillStyle = gradient;
    context.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }

  function makeScene() {
    random = seedFor(`${config.effect}:${Math.round(width)}:${Math.round(height)}`);
    scene = [];
    const counts = { matrix: Math.ceil(width / 20), hearth: 76, aurora: 100, starfield: 260, aquarium: 48, lava: 9, rain: 160 };
    for (let index = 0; index < counts[config.effect]; index += 1) {
      scene.push({ x: random(), y: random(), speed: random(), size: random(), phase: random() * TAU, color: random(), index });
    }
  }

  function matrix(time) {
    background();
    const fontSize = width < 600 ? 15 : 17;
    const step = fontSize * 1.25;
    context.font = `500 ${fontSize}px ui-monospace, Menlo, monospace`;
    context.textAlign = "center";
    for (const stream of scene) {
      const head = ((stream.y * (height + 350) + time * (25 + stream.speed * 48)) % (height + 350)) - 100;
      const x = stream.index * 20 + 9;
      const length = 7 + Math.floor(stream.size * 15);
      for (let tail = 0; tail < length; tail += 1) {
        const y = head - tail * step;
        if (y < -step || y > height + step) continue;
        const alpha = (1 - tail / length) * (dark() ? 0.65 : 0.31);
        context.fillStyle = tail === 0 ? (dark() ? "#ceffdf" : "#086433") : rgba(dark() ? "55,230,113" : "22,116,59", alpha);
        const charIndex = Math.abs(Math.floor(stream.phase * 23 + tail * 17 + Math.floor(time * (tail === 0 ? 4 : 0.4))));
        context.fillText(glyphs[charIndex % glyphs.length], x, y);
      }
    }
    context.textAlign = "start";
    const shade = context.createLinearGradient(0, 0, width, 0);
    shade.addColorStop(0, rgba(dark() ? "0,10,3" : "237,248,237", 0));
    shade.addColorStop(0.5, rgba(dark() ? "0,10,3" : "237,248,237", 0.22));
    shade.addColorStop(1, rgba(dark() ? "0,10,3" : "237,248,237", 0));
    context.fillStyle = shade;
    context.fillRect(0, 0, width, height);
  }

  function hearth(time) {
    background(dark() ? "rgba(18,7,4,.4)" : "rgba(255,239,213,.35)", dark() ? "rgba(32,10,3,.9)" : "rgba(228,175,117,.28)");
    glow(width * 0.5, height * 1.02, Math.min(width * 0.8, height * 0.9), "242,85,18", dark() ? 0.31 : 0.15);
    const floor = height * 0.96;
    // Carry the outer flames into the visible side margins of a full board.
    const baseWidth = Math.min(1650, width * 1.18);
    const start = (width - baseWidth) / 2;
    for (let layer = 0; layer < 3; layer += 1) {
      for (let index = 0; index < 17; index += 1) {
        const phase = index * 1.91 + layer * 0.7;
        const x = start + baseWidth * index / 16 + Math.sin(time * 1.3 + phase) * 13;
        const peak = floor - (85 + Math.sin(time * 2.2 + phase) * 32 + (1 - Math.abs(index - 8) / 10) * height * (0.18 + layer * 0.03));
        const spread = baseWidth / 13 + layer * 3;
        const sway = Math.sin(time * 1.6 + phase) * 32;
        const gradient = context.createLinearGradient(0, peak, 0, floor);
        gradient.addColorStop(0, rgba(layer === 0 ? "255,71,13" : "255,173,37", 0));
        gradient.addColorStop(0.35, rgba(layer === 0 ? "224,51,10" : layer === 1 ? "255,119,15" : "255,208,87", dark() ? 0.36 : 0.27));
        gradient.addColorStop(1, rgba(layer === 0 ? "222,48,8" : layer === 1 ? "255,150,27" : "255,225,141", 0.83));
        context.fillStyle = gradient;
        context.beginPath();
        context.moveTo(x - spread, floor);
        context.bezierCurveTo(x - spread * 0.7, floor - 80, x + sway - spread * 0.5, peak + 36, x + sway, peak);
        context.bezierCurveTo(x + sway - 3, peak + 95, x + spread, floor - 50, x + spread, floor);
        context.closePath();
        context.fill();
      }
    }
    // Two charred logs make this a hearth rather than an abstract orange glow.
    context.save();
    context.translate(width / 2, floor + 6);
    for (const angle of [-0.08, 0.08]) {
      context.save();
      context.rotate(angle);
      const logGradient = context.createLinearGradient(0, -14, 0, 15);
      logGradient.addColorStop(0, "#6b2b12");
      logGradient.addColorStop(0.35, "#28120b");
      logGradient.addColorStop(1, "#120c09");
      context.fillStyle = logGradient;
      context.beginPath();
      context.roundRect(-baseWidth * 0.38, -10, baseWidth * 0.76, 28, 12);
      context.fill();
      context.strokeStyle = "rgba(255,112,31,.42)";
      context.lineWidth = 1;
      for (let crack = -3; crack <= 3; crack += 1) {
        context.beginPath();
        context.moveTo(baseWidth * crack / 11, -7);
        context.lineTo(baseWidth * crack / 11 + 12, 3);
        context.lineTo(baseWidth * crack / 11 - 3, 11);
        context.stroke();
      }
      context.restore();
    }
    context.restore();
    for (const ember of scene) {
      const age = (ember.y + time * (0.018 + ember.speed * 0.024)) % 1;
      const x = start + ember.x * baseWidth + Math.sin(age * 8 + ember.phase) * (15 + age * 50);
      const y = floor - age * height * 0.76;
      context.fillStyle = rgba("255,164,59", (1 - age) * (dark() ? 0.85 : 0.65));
      context.beginPath();
      context.ellipse(x, y, 0.7 + ember.size * 1.6, 1 + ember.size * 2.4, 0.3, 0, TAU);
      context.fill();
    }
  }

  function aurora(time) {
    background(dark() ? "rgba(3,11,30,.6)" : "rgba(217,240,248,.35)", dark() ? "rgba(6,19,26,.22)" : "rgba(235,237,250,.2)");
    for (const star of scene) {
      context.fillStyle = rgba(dark() ? "194,223,235" : "71,122,133", dark() ? 0.25 + star.size * 0.4 : 0.16);
      context.fillRect(star.x * width, star.y * height * 0.8, 1 + star.size, 1 + star.size);
    }
    const colors = ["59,238,160", "60,203,217", "138,107,233", "214,124,225"];
    for (let ribbon = 0; ribbon < 4; ribbon += 1) {
      for (let fold = 21; fold >= 0; fold -= 1) {
        context.beginPath();
        for (let step = 0; step <= 80; step += 1) {
          const x = width * step / 80;
          const wave = Math.sin(step / 12 + time * 0.12 + ribbon * 1.6) * height * 0.08 + Math.sin(step / 6 - time * 0.15 + ribbon) * height * 0.035;
          const y = height * (0.16 + ribbon * 0.11) + wave + fold * 3.6;
          if (!step) context.moveTo(x, y); else context.lineTo(x, y);
        }
        context.lineWidth = 6;
        context.strokeStyle = rgba(colors[ribbon], (1 - fold / 24) * (dark() ? 0.06 : 0.035));
        context.stroke();
      }
    }
    glow(width * 0.2, height * 0.22, width * 0.4, dark() ? "28,138,101" : "91,198,151", dark() ? 0.11 : 0.12);
  }

  function starfield(time) {
    background(dark() ? "rgba(5,4,20,.64)" : "rgba(229,233,250,.3)", dark() ? "rgba(13,5,28,.5)" : "rgba(241,228,248,.2)");
    glow(width * 0.22, height * 0.35, Math.max(width, height) * 0.5, "85,60,154", dark() ? 0.22 : 0.1);
    glow(width * 0.81, height * 0.7, width * 0.5, "36,105,162", dark() ? 0.16 : 0.09);
    for (const star of scene) {
      const depth = (star.speed + time * 0.007) % 1;
      const scale = 0.6 + depth * 1.1;
      const x = width * 0.5 + (star.x - 0.5) * width * scale;
      const y = height * 0.46 + (star.y - 0.46) * height * scale;
      if (x < 0 || x > width || y < 0 || y > height) continue;
      const alpha = 0.1 + Math.sin(depth * Math.PI) * (dark() ? 0.7 : 0.45);
      const size = 0.4 + star.size * 1.5 * depth;
      const rgb = dark() ? (star.color > 0.7 ? "209,187,255" : "204,228,255") : "66,71,126";
      context.fillStyle = rgba(rgb, alpha);
      context.beginPath();
      context.arc(x, y, size, 0, TAU);
      context.fill();
      if (star.size > 0.91 && depth > 0.4) {
        context.strokeStyle = rgba(rgb, alpha * 0.4);
        context.lineWidth = 0.7;
        context.beginPath();
        context.moveTo(x - size * 3, y);
        context.lineTo(x + size * 3, y);
        context.moveTo(x, y - size * 3);
        context.lineTo(x, y + size * 3);
        context.stroke();
      }
    }
    const planetX = width * 0.88;
    const planetY = height * 0.8;
    const radius = Math.min(105, width * 0.095);
    const planet = context.createRadialGradient(planetX - radius * 0.35, planetY - radius * 0.45, 1, planetX, planetY, radius);
    planet.addColorStop(0, dark() ? "rgba(137,108,173,.65)" : "rgba(160,145,193,.38)");
    planet.addColorStop(0.75, dark() ? "rgba(59,46,93,.5)" : "rgba(181,171,205,.28)");
    planet.addColorStop(1, dark() ? "rgba(18,13,37,.3)" : "rgba(206,199,224,.1)");
    context.fillStyle = planet;
    context.beginPath();
    context.arc(planetX, planetY, radius, 0, TAU);
    context.fill();
    context.save();
    context.translate(planetX, planetY);
    context.rotate(-0.3);
    context.strokeStyle = dark() ? "rgba(164,148,197,.32)" : "rgba(115,101,151,.22)";
    context.lineWidth = radius * 0.09;
    context.beginPath();
    context.ellipse(0, 0, radius * 1.6, radius * 0.38, 0, 0, TAU);
    context.stroke();
    context.restore();
  }

  function aquarium(time) {
    background(dark() ? "rgba(3,31,48,.6)" : "rgba(167,227,232,.32)", dark() ? "rgba(2,22,34,.85)" : "rgba(132,195,198,.28)");
    for (let ray = 0; ray < 6; ray += 1) {
      const x = (ray / 5) * width + Math.sin(time * 0.2 + ray) * 35;
      const beam = context.createLinearGradient(0, 0, 0, height);
      beam.addColorStop(0, rgba("170,244,247", dark() ? 0.07 : 0.13));
      beam.addColorStop(1, "rgba(170,244,247,0)");
      context.fillStyle = beam;
      context.beginPath();
      context.moveTo(x, 0);
      context.lineTo(x + 45, 0);
      context.lineTo(x + height * 0.43, height);
      context.lineTo(x + height * 0.18, height);
      context.closePath();
      context.fill();
    }
    for (const item of scene) {
      if (item.index < 14) {
        const direction = item.index % 2 ? 1 : -1;
        const x = ((item.x * (width + 140) + direction * time * (7 + item.speed * 13)) % (width + 140) + width + 140) % (width + 140) - 70;
        const y = height * (0.17 + item.y * 0.66) + Math.sin(time * 0.8 + item.phase) * 7;
        const size = 10 + item.size * 20;
        context.save();
        context.translate(x, y);
        context.scale(direction, 1);
        const rgb = item.color < 0.34 ? "239,165,92" : item.color < 0.66 ? "99,190,204" : "170,154,212";
        context.fillStyle = rgba(rgb, dark() ? 0.58 : 0.52);
        context.beginPath();
        context.ellipse(0, 0, size, size * 0.43, 0, 0, TAU);
        context.moveTo(-size * 0.85, 0);
        const tail = Math.sin(time * 4 + item.phase) * 0.1;
        context.lineTo(-size * 1.65, -size * (0.45 + tail));
        context.lineTo(-size * 1.65, size * (0.45 + tail));
        context.closePath();
        context.fill();
        context.fillStyle = rgba(dark() ? "222,252,249" : "8,70,81", 0.8);
        context.beginPath();
        context.arc(size * 0.58, -size * 0.09, 1.4, 0, TAU);
        context.fill();
        context.restore();
      } else {
        const age = (item.y + time * (0.012 + item.speed * 0.02)) % 1;
        const x = item.x * width + Math.sin(age * 8 + item.phase) * 13;
        const y = height * (1.04 - age * 1.1);
        const radius = 1.5 + item.size * 5;
        context.strokeStyle = rgba(dark() ? "157,230,239" : "40,125,146", 0.17 + item.size * 0.18);
        context.lineWidth = 1;
        context.beginPath();
        context.arc(x, y, radius, 0, TAU);
        context.stroke();
        context.fillStyle = rgba("237,255,255", dark() ? 0.25 : 0.4);
        context.beginPath();
        context.arc(x - radius * 0.35, y - radius * 0.35, Math.max(0.7, radius * 0.18), 0, TAU);
        context.fill();
      }
    }
    for (let plant = 0; plant < 30; plant += 1) {
      const x = plant * width / 29;
      const stem = 25 + Math.sin(plant * 8.3) ** 2 * height * 0.14;
      context.strokeStyle = rgba(dark() ? "56,119,107" : "61,134,120", 0.35);
      context.lineWidth = 3 + (plant % 3);
      context.beginPath();
      context.moveTo(x, height);
      context.bezierCurveTo(x - 12, height - stem * 0.33, x + Math.sin(time * 0.7 + plant) * 18, height - stem * 0.8, x + Math.sin(time * 0.7 + plant) * 24, height - stem);
      context.stroke();
    }
  }

  function lava(time) {
    background(dark() ? "rgba(25,3,33,.7)" : "rgba(255,231,223,.28)", dark() ? "rgba(32,7,24,.5)" : "rgba(242,213,234,.25)");
    const colors = ["254,94,69", "255,166,58", "210,74,177", "160,80,213"];
    glow(width * 0.28, height, width * 0.5, "255,95,28", dark() ? 0.15 : 0.1);
    for (const blob of scene) {
      const cycle = (blob.y + time * (0.006 + blob.speed * 0.009)) % 1;
      const radius = Math.min(width, height) * (0.075 + blob.size * 0.1);
      const x = width * (0.08 + blob.x * 0.84) + Math.sin(time * 0.13 + blob.phase) * width * 0.035;
      const y = height + radius - cycle * (height + radius * 2);
      const rgb = colors[Math.floor(blob.color * colors.length)];
      const gradient = context.createRadialGradient(x - radius * 0.3, y - radius * 0.4, 0, x, y, radius * 1.2);
      gradient.addColorStop(0, rgba(rgb, dark() ? 0.68 : 0.4));
      gradient.addColorStop(0.7, rgba(rgb, dark() ? 0.5 : 0.3));
      gradient.addColorStop(1, rgba(rgb, dark() ? 0.17 : 0.08));
      context.fillStyle = gradient;
      context.beginPath();
      const points = [];
      for (let point = 0; point < 16; point += 1) {
        const angle = point * TAU / 16;
        const wobble = 1 + Math.sin(angle * 3 + time * 0.55 + blob.phase) * 0.13 + Math.cos(angle * 2 - time * 0.35) * 0.13;
        points.push([x + Math.cos(angle) * radius * wobble, y + Math.sin(angle) * radius * wobble * (1.1 + blob.speed * 0.7)]);
      }
      const first = points[0];
      const last = points[points.length - 1];
      context.moveTo((first[0] + last[0]) / 2, (first[1] + last[1]) / 2);
      for (let point = 0; point < points.length; point += 1) {
        const current = points[point];
        const next = points[(point + 1) % points.length];
        context.quadraticCurveTo(current[0], current[1], (current[0] + next[0]) / 2, (current[1] + next[1]) / 2);
      }
      context.closePath();
      context.fill();
    }
  }

  function rain(time) {
    background(dark() ? "rgba(12,25,38,.55)" : "rgba(200,219,227,.4)", dark() ? "rgba(15,29,43,.75)" : "rgba(181,201,213,.35)");
    for (let light = 0; light < 9; light += 1) {
      const x = (light * 0.138 + 0.03) * width;
      const y = height * (0.7 + Math.sin(light * 9) * 0.16);
      glow(x, y, 35 + (light % 3) * 25, light % 2 ? "236,166,110" : "103,170,210", dark() ? 0.1 : 0.07);
    }
    context.lineCap = "round";
    for (const drop of scene) {
      const cycle = (drop.y + time * (0.23 + drop.speed * 0.35)) % 1;
      const x = ((drop.x * width - cycle * 45 + width) % width);
      const y = cycle * (height + 80) - 40;
      const length = 9 + drop.size * 33;
      context.strokeStyle = rgba(dark() ? "157,193,212" : "79,124,150", 0.07 + drop.size * 0.15);
      context.lineWidth = 0.6 + drop.size;
      context.beginPath();
      context.moveTo(x, y);
      context.lineTo(x - length * 0.13, y + length);
      context.stroke();
      if (drop.index < 24) {
        const glassY = ((drop.y + time * 0.007 * (0.3 + drop.speed)) % 1) * height;
        const glassX = drop.x * width;
        const radius = 3 + drop.size * 6;
        context.strokeStyle = rgba(dark() ? "189,222,235" : "55,111,143", dark() ? 0.18 : 0.24);
        context.lineWidth = 0.8;
        context.beginPath();
        context.moveTo(glassX, glassY - radius * 1.6);
        context.bezierCurveTo(glassX + radius * 0.4, glassY - radius * 0.3, glassX + radius * 1.25, glassY + radius * 0.6, glassX, glassY + radius);
        context.bezierCurveTo(glassX - radius * 1.25, glassY + radius * 0.6, glassX - radius * 0.4, glassY - radius * 0.3, glassX, glassY - radius * 1.6);
        context.stroke();
        context.strokeStyle = rgba("218,245,252", dark() ? 0.25 : 0.5);
        context.beginPath();
        context.arc(glassX, glassY + radius * 0.2, radius * 0.7, 0.3, 1.3);
        context.stroke();
      }
    }
    context.lineCap = "butt";
  }

  const painters = { matrix, hearth, aurora, starfield, aquarium, lava, rain };

  function paint() {
    if (!context || !config) return;
    context.globalAlpha = 1;
    context.globalCompositeOperation = "source-over";
    painters[config.effect](elapsed);
  }

  function shouldAnimate() {
    return canvas && config && config.motion && !reducedMotion.matches && !document.hidden;
  }

  function tick(now) {
    frame = 0;
    if (!shouldAnimate()) return;
    if (!lastFrame || now - lastFrame >= 1000 / 24) {
      elapsed += lastFrame ? Math.min((now - lastFrame) / 1000, 0.12) : 0;
      lastFrame = now;
      paint();
    }
    frame = requestAnimationFrame(tick);
  }

  function restart() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    lastFrame = 0;
    if (!document.hidden) paint();
    if (shouldAnimate()) frame = requestAnimationFrame(tick);
  }

  function resize() {
    if (!canvas || !context) return;
    width = Math.max(1, window.innerWidth);
    height = Math.max(1, window.innerHeight);
    // Large external displays should not turn a decorative layer into a GPU load.
    const ratio = Math.min(window.devicePixelRatio || 1, 1.35, Math.sqrt(2100000 / (width * height)));
    canvas.width = Math.max(1, Math.round(width * ratio));
    canvas.height = Math.max(1, Math.round(height * ratio));
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    makeScene();
    restart();
  }

  function clear() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    lastFrame = 0;
    canvas?.remove();
    canvas = null;
    context = null;
    config = null;
    scene = [];
    delete document.documentElement.dataset.effect;
    window.removeEventListener("resize", resize);
    document.removeEventListener("visibilitychange", restart);
  }

  function configure(options = {}) {
    const effect = effectNames.has(options.effect) ? options.effect : null;
    if (!effect) { clear(); return; }
    const next = { effect, mode: options.mode === "light" ? "light" : "dark", motion: options.motion !== false, themeId: String(options.themeId || effect) };
    if (config && Object.keys(next).every(key => next[key] === config[key])) return;
    const old = config;
    config = next;
    paper = getComputedStyle(document.documentElement).getPropertyValue("--paper").trim() || (dark() ? "#050805" : "#f5f4ef");
    document.documentElement.dataset.effect = effect;
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.id = "board-effects";
      canvas.className = "board-effects";
      canvas.setAttribute("aria-hidden", "true");
      canvas.setAttribute("role", "presentation");
      document.body.prepend(canvas);
      context = canvas.getContext("2d", { alpha: false });
      if (!context) { clear(); return; }
      window.addEventListener("resize", resize, { passive: true });
      document.addEventListener("visibilitychange", restart);
      elapsed = 18;
      resize();
    } else if (!old || old.effect !== next.effect) {
      elapsed = 18;
      makeScene();
      restart();
    } else {
      restart();
    }
  }

  if (reducedMotion.addEventListener) reducedMotion.addEventListener("change", restart);
  else reducedMotion.addListener(restart);
  window.AgentBoardEffects = Object.freeze({ configure, clear });
})();
