function setProgress(percent, message) {
  const progress = document.getElementById("jobProgress");
  const bar = document.getElementById("progressBar");
  const label = document.getElementById("jobPercent");
  const copy = document.getElementById("jobMessage");

  if (!progress || !bar || !label || !copy) return;

  progress.classList.remove("hidden");
  const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
  bar.style.width = `${safePercent}%`;
  label.textContent = `${safePercent}%`;
  copy.textContent = message || "Processing...";
}

async function parseJsonResponse(response) {
  const text = await response.text();

  if (!text.trim()) {
    throw new Error(
      response.ok
        ? "Server returned an empty response."
        : `Server returned ${response.status} without error details.`
    );
  }

  try {
    return JSON.parse(text);
  } catch (error) {
    const details = text.replace(/\s+/g, " ").trim().slice(0, 180);
    throw new Error(
      response.ok
        ? "Server returned an invalid response."
        : `Server error ${response.status}: ${details}`
    );
  }
}

async function pollJob(statusUrl) {
  const response = await fetch(statusUrl);
  const job = await parseJsonResponse(response);

  if (!response.ok) {
    throw new Error(job.error || "Could not read job status.");
  }

  setProgress(job.progress, job.message);

  if (job.status === "completed") {
    window.location.href = job.result_url;
    return;
  }

  if (job.status === "failed") {
    setProgress(job.progress || 100, job.error || "Processing failed.");
    return;
  }

  setTimeout(() => pollJob(statusUrl).catch(showUploadError), 1400);
}

function showUploadError(error) {
  setProgress(100, error.message || "Something went wrong.");
}

async function startUpload(formData) {
  const uploadForm = document.getElementById("uploadForm");
  if (!uploadForm) return;

  setProgress(8, "Uploading audio...");

  const response = await fetch(uploadForm.action, {
    method: "POST",
    body: formData,
    headers: { "X-Requested-With": "XMLHttpRequest" },
  });
  const payload = await parseJsonResponse(response);

  if (!response.ok) {
    throw new Error(payload.error || "Upload failed.");
  }

  setProgress(12, "Queued for transcription...");
  pollJob(payload.status_url).catch(showUploadError);
}

function setupUploadForm() {
  const uploadForm = document.getElementById("uploadForm");
  const audioFile = document.getElementById("audioFile");
  const fileName = document.getElementById("fileName");
  const fileDrop = document.getElementById("fileDrop");

  if (!uploadForm || !audioFile) return;

  audioFile.addEventListener("change", () => {
    if (audioFile.files.length && fileName) {
      fileName.textContent = audioFile.files[0].name;
    }
  });

  if (fileDrop) {
    ["dragenter", "dragover"].forEach((eventName) => {
      fileDrop.addEventListener(eventName, (event) => {
        event.preventDefault();
        fileDrop.classList.add("drag-over");
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      fileDrop.addEventListener(eventName, (event) => {
        event.preventDefault();
        fileDrop.classList.remove("drag-over");
      });
    });

    fileDrop.addEventListener("drop", (event) => {
      if (!event.dataTransfer.files.length) return;
      audioFile.files = event.dataTransfer.files;
      if (fileName) fileName.textContent = event.dataTransfer.files[0].name;
    });
  }

  uploadForm.addEventListener("submit", (event) => {
    event.preventDefault();
    startUpload(new FormData(uploadForm)).catch(showUploadError);
  });
}

function setupRecorder() {
  const startBtn = document.getElementById("startBtn");
  const stopBtn = document.getElementById("stopBtn");
  const timer = document.getElementById("timer");
  const uploadForm = document.getElementById("uploadForm");

  if (!startBtn || !stopBtn || !timer || !uploadForm) return;

  let mediaRecorder;
  let audioChunks = [];
  let timerInterval;
  let seconds = 0;
  let stream;

  const updateTimer = () => {
    seconds += 1;
    const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
    const secs = String(seconds % 60).padStart(2, "0");
    timer.textContent = `${minutes}:${secs}`;
  };

  startBtn.addEventListener("click", async () => {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    seconds = 0;
    timer.textContent = "00:00";

    const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    mediaRecorder.ondataavailable = (event) => audioChunks.push(event.data);
    mediaRecorder.start();

    startBtn.disabled = true;
    stopBtn.disabled = false;
    timerInterval = setInterval(updateTimer, 1000);
  });

  stopBtn.addEventListener("click", () => {
    if (!mediaRecorder) return;

    mediaRecorder.onstop = () => {
      clearInterval(timerInterval);
      if (stream) stream.getTracks().forEach((track) => track.stop());

      const blob = new Blob(audioChunks, { type: "audio/webm" });
      const file = new File([blob], "recorded_audio.webm", { type: "audio/webm" });
      const formData = new FormData(uploadForm);
      formData.set("audiofile", file);

      startBtn.disabled = false;
      stopBtn.disabled = true;
      startUpload(formData).catch(showUploadError);
    };

    mediaRecorder.stop();
  });
}

function setupCopyButtons() {
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;

      await navigator.clipboard.writeText(target.innerText.trim());
      const oldText = button.innerHTML;
      button.innerHTML = '<i class="fa-solid fa-check"></i> Copied';
      setTimeout(() => {
        button.innerHTML = oldText;
      }, 1400);
    });
  });
}

function setupQa() {
  const form = document.getElementById("qaForm");
  const answerBox = document.getElementById("qaAnswer");
  if (!form || !answerBox) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(form);
    const question = String(formData.get("question") || "").trim();
    if (!question) return;

    answerBox.classList.remove("hidden");
    answerBox.textContent = "Thinking through the transcript...";

    let response;
    let payload;
    try {
      response = await fetch(`/api/ask/${form.dataset.jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      payload = await parseJsonResponse(response);
    } catch (error) {
      answerBox.textContent = error.message || "Could not answer that question.";
      return;
    }

    if (!response.ok) {
      answerBox.textContent = payload.error || "Could not answer that question.";
      return;
    }

    const windows = payload.context_windows && payload.context_windows.length
      ? `Context: ${payload.context_windows.join(", ")}`
      : "";
    answerBox.textContent = payload.answer || "No answer returned.";
    const meta = document.createElement("small");
    meta.textContent = `${payload.source}${windows ? ` • ${windows}` : ""}`;
    answerBox.appendChild(meta);
  });
}

function setupCursorGlow() {
  const glow = document.getElementById("cursorGlow");
  if (!glow || window.matchMedia("(pointer: coarse)").matches) return;

  let frame;
  document.body.classList.add("has-pointer");

  window.addEventListener("pointermove", (event) => {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const x = `${event.clientX}px`;
      const y = `${event.clientY}px`;
      glow.style.left = x;
      glow.style.top = y;
      document.documentElement.style.setProperty("--mouse-x", x);
      document.documentElement.style.setProperty("--mouse-y", y);
    });
  });
}

function setupReveals() {
  const revealItems = document.querySelectorAll(".reveal");
  if (!revealItems.length) return;

  if (!("IntersectionObserver" in window)) {
    revealItems.forEach((item) => item.classList.add("revealed"));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("revealed");
        observer.unobserve(entry.target);
      });
    },
    { threshold: 0.14 }
  );

  revealItems.forEach((item, index) => {
    item.style.transitionDelay = `${Math.min(index * 45, 220)}ms`;
    observer.observe(item);
  });
}

function setupTiltCards() {
  const cards = document.querySelectorAll(".panel, .metric-card, .auth-card");
  if (!cards.length || window.matchMedia("(pointer: coarse)").matches) return;

  cards.forEach((card) => {
    card.addEventListener("pointermove", (event) => {
      const rect = card.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width - 0.5;
      const y = (event.clientY - rect.top) / rect.height - 0.5;
      card.style.setProperty("--tilt-x", `${(-y * 3).toFixed(2)}deg`);
      card.style.setProperty("--tilt-y", `${(x * 3).toFixed(2)}deg`);
      card.style.transform = "perspective(900px) rotateX(var(--tilt-x)) rotateY(var(--tilt-y))";
    });

    card.addEventListener("pointerleave", () => {
      card.style.transform = "";
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupCursorGlow();
  setupReveals();
  setupTiltCards();
  setupUploadForm();
  setupRecorder();
  setupCopyButtons();
  setupQa();
});
