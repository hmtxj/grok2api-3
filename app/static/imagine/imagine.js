(() => {
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const clearBtn = document.getElementById('clearBtn');
  const promptInput = document.getElementById('promptInput');
  const ratioSelect = document.getElementById('ratioSelect');
  const concurrentSelect = document.getElementById('concurrentSelect');
  const autoScrollToggle = document.getElementById('autoScrollToggle');
  const autoDownloadToggle = document.getElementById('autoDownloadToggle');
  const filterBlurToggle = document.getElementById('filterBlurToggle');
  const selectFolderBtn = document.getElementById('selectFolderBtn');
  const folderPath = document.getElementById('folderPath');
  const statusText = document.getElementById('statusText');
  const countValue = document.getElementById('countValue');
  const activeValue = document.getElementById('activeValue');
  const latencyValue = document.getElementById('latencyValue');
  const filteredValue = document.getElementById('filteredValue');
  const modeButtons = document.querySelectorAll('.mode-btn');
  const waterfall = document.getElementById('waterfall');
  const emptyState = document.getElementById('emptyState');
  const lightbox = document.getElementById('lightbox');
  const lightboxImg = document.getElementById('lightboxImg');
  const closeLightbox = document.getElementById('closeLightbox');

  let wsConnections = [];
  let sseConnections = [];
  let imageCount = 0;
  let filteredCount = 0;
  let totalLatency = 0;
  let latencyCount = 0;
  let lastRunId = '';
  let isRunning = false;
  let connectionMode = 'ws';
  let modePreference = 'auto';
  const MODE_STORAGE_KEY = 'imagine_mode';
  const FILTER_BLUR_KEY = 'imagine_filter_blur';
  let pendingFallbackTimer = null;
  let currentTaskIds = [];
  let directoryHandle = null;
  let useFileSystemAPI = false;
  let isSelectionMode = false;
  let selectedImages = new Set();

  // === 编辑模式状态 (NEW) ===
  let imagineMode = 'generate'; // 'generate' 或 'edit'
  let editImageFile = null;
  const imagineModeBtns = document.querySelectorAll('.imagine-mode-btn');
  const editImageUpload = document.getElementById('editImageUpload');
  const editUploadArea = document.getElementById('editUploadArea');
  const editFileInput = document.getElementById('editFileInput');
  const editUploadPlaceholder = document.getElementById('editUploadPlaceholder');
  const editPreviewContainer = document.getElementById('editPreviewContainer');
  const editPreviewImg = document.getElementById('editPreviewImg');
  const editRemoveBtn = document.getElementById('editRemoveBtn');

  // === 动态随机状态 ===
  const dynamicRandomToggle = document.getElementById('dynamicRandomToggle');
  const wildcardTagsContainer = document.getElementById('wildcardTags');

  function toast(message, type) {
    if (typeof showToast === 'function') {
      showToast(message, type);
    }
  }

  function setStatus(state, text) {
    if (!statusText) return;
    statusText.textContent = text;
    statusText.classList.remove('connected', 'connecting', 'error');
    if (state) {
      statusText.classList.add(state);
    }
  }

  function setButtons(connected) {
    if (!startBtn || !stopBtn) return;
    if (connected) {
      startBtn.classList.add('hidden');
      stopBtn.classList.remove('hidden');
    } else {
      startBtn.classList.remove('hidden');
      stopBtn.classList.add('hidden');
      startBtn.disabled = false;
    }
  }

  function updateCount(value) {
    if (countValue) {
      countValue.textContent = String(value);
    }
  }

  function updateActive() {
    if (!activeValue) return;
    if (connectionMode === 'sse') {
      const active = sseConnections.filter(es => es && es.readyState === EventSource.OPEN).length;
      activeValue.textContent = String(active);
      return;
    }
    const active = wsConnections.filter(ws => ws && ws.readyState === WebSocket.OPEN).length;
    activeValue.textContent = String(active);
  }

  function setModePreference(mode, persist = true) {
    if (!['auto', 'ws', 'sse'].includes(mode)) return;
    modePreference = mode;
    modeButtons.forEach(btn => {
      if (btn.dataset.mode === mode) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });
    if (persist) {
      try {
        localStorage.setItem(MODE_STORAGE_KEY, mode);
      } catch (e) {
        // ignore
      }
    }
    updateModeValue();
  }

  function updateModeValue() { }


  function updateLatency(value) {
    if (value) {
      totalLatency += value;
      latencyCount += 1;
      const avg = Math.round(totalLatency / latencyCount);
      if (latencyValue) {
        latencyValue.textContent = `${avg} ms`;
      }
    } else {
      if (latencyValue) {
        latencyValue.textContent = '-';
      }
    }
  }

  function updateError(value) { }

  function inferMime(base64) {
    if (!base64) return 'image/jpeg';
    if (base64.startsWith('iVBOR')) return 'image/png';
    if (base64.startsWith('/9j/')) return 'image/jpeg';
    if (base64.startsWith('R0lGOD')) return 'image/gif';
    return 'image/jpeg';
  }

  // 相邻像素差异分析法检测模糊打码图
  function isBlurry(base64) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        try {
          const MAX_W = 100;
          const scale = Math.min(1, MAX_W / img.width);
          const w = Math.round(img.width * scale);
          const h = Math.round(img.height * scale);

          const canvas = document.createElement('canvas');
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);
          const data = ctx.getImageData(0, 0, w, h).data;

          // 转灰度
          const gray = new Uint8Array(w * h);
          for (let i = 0; i < w * h; i++) {
            const off = i * 4;
            gray[i] = Math.round(0.299 * data[off] + 0.587 * data[off + 1] + 0.114 * data[off + 2]);
          }

          // 计算每个像素与右侧/下方像素的灰度绝对差
          let totalDiff = 0, sharpPixels = 0, pairs = 0;
          const SHARP_THRESHOLD = 8;

          for (let y = 0; y < h; y++) {
            for (let x = 0; x < w; x++) {
              const idx = y * w + x;
              // 与右侧像素的差异
              if (x < w - 1) {
                const diff = Math.abs(gray[idx] - gray[idx + 1]);
                totalDiff += diff;
                if (diff > SHARP_THRESHOLD) sharpPixels++;
                pairs++;
              }
              // 与下方像素的差异
              if (y < h - 1) {
                const diff = Math.abs(gray[idx] - gray[idx + w]);
                totalDiff += diff;
                if (diff > SHARP_THRESHOLD) sharpPixels++;
                pairs++;
              }
            }
          }

          if (pairs === 0) { resolve(false); return; }

          const avgDiff = totalDiff / pairs;
          const sharpRatio = (sharpPixels / pairs) * 100;

          // 模糊打码图：相邻像素差异极小，几乎没有锐利像素
          const isBlur = avgDiff < 4 || sharpRatio < 3;

          console.log(
            `[模糊检测] avgDiff=${avgDiff.toFixed(2)}, sharpRatio=${sharpRatio.toFixed(1)}% → ${isBlur ? '🚫 模糊' : '✅ 正常'}`
          );

          resolve(isBlur);
        } catch (e) {
          resolve(false);
        }
      };
      img.onerror = () => resolve(false);
      const mime = inferMime(base64);
      img.src = `data:${mime};base64,${base64}`;
    });
  }

  function updateFiltered(value) {
    if (filteredValue) {
      filteredValue.textContent = String(value);
    }
  }

  function dataUrlToBlob(dataUrl) {
    const parts = (dataUrl || '').split(',');
    if (parts.length < 2) return null;
    const header = parts[0];
    const b64 = parts.slice(1).join(',');
    const match = header.match(/data:(.*?);base64/);
    const mime = match ? match[1] : 'application/octet-stream';
    try {
      const byteString = atob(b64);
      const ab = new ArrayBuffer(byteString.length);
      const ia = new Uint8Array(ab);
      for (let i = 0; i < byteString.length; i++) {
        ia[i] = byteString.charCodeAt(i);
      }
      return new Blob([ab], { type: mime });
    } catch (e) {
      return null;
    }
  }

  async function createImagineTask(prompt, ratio, apiKey) {
    const dynamicRandom = dynamicRandomToggle ? dynamicRandomToggle.checked : false;
    const res = await fetch('/api/v1/admin/imagine/start', {
      method: 'POST',
      headers: {
        ...buildAuthHeaders(apiKey),
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ prompt, aspect_ratio: ratio, dynamic_random: dynamicRandom })
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || 'Failed to create task');
    }
    const data = await res.json();
    return data && data.task_id ? String(data.task_id) : '';
  }

  async function createImagineTasks(prompt, ratio, concurrent, apiKey) {
    const tasks = [];
    for (let i = 0; i < concurrent; i++) {
      const taskId = await createImagineTask(prompt, ratio, apiKey);
      if (!taskId) {
        throw new Error('Missing task id');
      }
      tasks.push(taskId);
    }
    return tasks;
  }

  async function stopImagineTasks(taskIds, apiKey) {
    if (!taskIds || taskIds.length === 0) return;
    try {
      await fetch('/api/v1/admin/imagine/stop', {
        method: 'POST',
        headers: {
          ...buildAuthHeaders(apiKey),
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ task_ids: taskIds })
      });
    } catch (e) {
      // ignore
    }
  }

  async function saveToFileSystem(base64, filename) {
    try {
      if (!directoryHandle) {
        return false;
      }

      const mime = inferMime(base64);
      const ext = mime === 'image/png' ? 'png' : 'jpg';
      const finalFilename = filename.endsWith(`.${ext}`) ? filename : `${filename}.${ext}`;

      const fileHandle = await directoryHandle.getFileHandle(finalFilename, { create: true });
      const writable = await fileHandle.createWritable();

      // Convert base64 to blob
      const byteString = atob(base64);
      const ab = new ArrayBuffer(byteString.length);
      const ia = new Uint8Array(ab);
      for (let i = 0; i < byteString.length; i++) {
        ia[i] = byteString.charCodeAt(i);
      }
      const blob = new Blob([ab], { type: mime });

      await writable.write(blob);
      await writable.close();
      return true;
    } catch (e) {
      console.error('File System API save failed:', e);
      return false;
    }
  }

  function downloadImage(base64, filename) {
    const mime = inferMime(base64);
    const dataUrl = `data:${mime};base64,${base64}`;
    const link = document.createElement('a');
    link.href = dataUrl;
    link.download = filename;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  async function appendImage(base64, meta) {
    if (!waterfall) return false;

    // 过滤空值和后端错误标记
    if (!base64 || base64 === 'error') return false;

    // 智能识别数据格式：URL / 完整 data URL / 纯 base64
    let dataUrl;
    if (base64.startsWith('http://') || base64.startsWith('https://')) {
      // 远程 URL，直接作为 img.src
      dataUrl = base64;
    } else if (base64.startsWith('data:')) {
      // 已经是完整的 data URL
      dataUrl = base64;
    } else {
      // 纯 base64 字符串
      if (base64.length < 100) return false;
      const mime = inferMime(base64);
      dataUrl = `data:${mime};base64,${base64}`;
    }

    // 预加载验证：确保数据能被浏览器解码为有效图片
    const isValid = await new Promise((resolve) => {
      const testImg = new Image();
      testImg.onload = () => resolve(testImg.naturalWidth > 0 && testImg.naturalHeight > 0);
      testImg.onerror = () => resolve(false);
      testImg.src = dataUrl;
    });
    if (!isValid) {
      console.warn('[appendImage] 无效图片数据，跳过渲染, 前20字符:', base64.substring(0, 20));
      return false;
    }

    // 开关开启时检测模糊图并跳过
    if (filterBlurToggle && filterBlurToggle.checked) {
      const blurry = await isBlurry(base64);
      if (blurry) {
        filteredCount++;
        updateFiltered(filteredCount);
        return false;
      }
    }

    if (emptyState) {
      emptyState.style.display = 'none';
    }

    const item = document.createElement('div');
    item.className = 'waterfall-item';

    const checkbox = document.createElement('div');
    checkbox.className = 'image-checkbox';

    const img = document.createElement('img');
    img.decoding = 'async';
    img.alt = meta && meta.sequence ? `image-${meta.sequence}` : 'image';
    // 加载失败时自动移除，防止显示坏图
    img.onerror = () => {
      if (item.parentNode) item.parentNode.removeChild(item);
    };
    img.src = dataUrl;

    const metaBar = document.createElement('div');
    metaBar.className = 'waterfall-meta';
    const left = document.createElement('div');
    left.textContent = meta && meta.sequence ? `#${meta.sequence}` : '#';
    const right = document.createElement('span');
    if (meta && meta.elapsed_ms) {
      right.textContent = `${meta.elapsed_ms}ms`;
    } else {
      right.textContent = '';
    }

    // 显示实际用于生成的 prompt（动态随机模式下会与原始 prompt 不同）
    if (meta && meta.prompt) {
      const promptTag = document.createElement('span');
      promptTag.className = 'meta-prompt-tag';
      promptTag.textContent = meta.prompt.length > 60 ? meta.prompt.substring(0, 60) + '...' : meta.prompt;
      promptTag.title = meta.prompt;
      metaBar.appendChild(promptTag);
    }

    metaBar.appendChild(left);
    metaBar.appendChild(right);

    item.appendChild(checkbox);
    item.appendChild(img);
    item.appendChild(metaBar);

    const prompt = (meta && meta.prompt) ? String(meta.prompt) : (promptInput ? promptInput.value.trim() : '');
    item.dataset.imageUrl = dataUrl;
    item.dataset.prompt = prompt || 'image';
    if (isSelectionMode) {
      item.classList.add('selection-mode');
    }

    waterfall.appendChild(item);

    if (autoScrollToggle && autoScrollToggle.checked) {
      window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
    }

    if (autoDownloadToggle && autoDownloadToggle.checked) {
      const timestamp = Date.now();
      const seq = meta && meta.sequence ? meta.sequence : 'unknown';
      const ext = dataUrl.includes('image/png') ? 'png' : 'jpg';
      const filename = `imagine_${timestamp}_${seq}.${ext}`;

      if (useFileSystemAPI && directoryHandle) {
        saveToFileSystem(base64, filename).catch(() => {
          downloadImage(base64, filename);
        });
      } else {
        downloadImage(base64, filename);
      }
    }
    return true;
  }

  function handleMessage(raw) {
    let data = null;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      return;
    }
    if (!data || typeof data !== 'object') return;

    if (data.type === 'image') {
      updateLatency(data.elapsed_ms);
      updateError('');
      appendImage(data.b64_json, data).then(ok => {
        if (ok) {
          imageCount += 1;
          updateCount(imageCount);
        }
      });
    } else if (data.type === 'status') {
      if (data.status === 'running') {
        setStatus('connected', '生成中');
        lastRunId = data.run_id || '';
      } else if (data.status === 'stopped') {
        if (data.run_id && lastRunId && data.run_id !== lastRunId) {
          return;
        }
        setStatus('', '已停止');
      }
    } else if (data.type === 'error') {
      const message = data.message || '生成失败';
      updateError(message);
      toast(message, 'error');
    }
  }

  function stopAllConnections() {
    wsConnections.forEach(ws => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        try {
          ws.send(JSON.stringify({ type: 'stop' }));
        } catch (e) {
          // ignore
        }
      }
      try {
        ws.close(1000, 'client stop');
      } catch (e) {
        // ignore
      }
    });
    wsConnections = [];

    sseConnections.forEach(es => {
      try {
        es.close();
      } catch (e) {
        // ignore
      }
    });
    sseConnections = [];
    updateActive();
    updateModeValue();
  }

  function buildSseUrl(taskId, index) {
    const httpProtocol = window.location.protocol === 'https:' ? 'https' : 'http';
    const base = `${httpProtocol}://${window.location.host}/api/v1/admin/imagine/sse`;
    const params = new URLSearchParams();
    params.set('task_id', taskId);
    params.set('t', String(Date.now()));
    if (typeof index === 'number') {
      params.set('conn', String(index));
    }
    return `${base}?${params.toString()}`;
  }

  function startSSE(taskIds) {
    connectionMode = 'sse';
    stopAllConnections();
    updateModeValue();

    setStatus('connected', '生成中 (SSE)');
    setButtons(true);
    toast(`已启动 ${taskIds.length} 个并发任务 (SSE)`, 'success');

    for (let i = 0; i < taskIds.length; i++) {
      const url = buildSseUrl(taskIds[i], i);
      const es = new EventSource(url);

      es.onopen = () => {
        updateActive();
      };

      es.onmessage = (event) => {
        handleMessage(event.data);
      };

      es.onerror = () => {
        updateActive();
        const remaining = sseConnections.filter(e => e && e.readyState === EventSource.OPEN).length;
        if (remaining === 0) {
          setStatus('error', '连接错误');
          setButtons(false);
          isRunning = false;
          startBtn.disabled = false;
          updateModeValue();
        }
      };

      sseConnections.push(es);
    }
  }

  async function startConnection() {
    const prompt = promptInput ? promptInput.value.trim() : '';
    if (!prompt) {
      toast('请输入提示词', 'error');
      return;
    }

    const apiKey = await ensureApiKey();
    if (apiKey === null) {
      toast('请先登录后台', 'error');
      return;
    }

    const concurrent = concurrentSelect ? parseInt(concurrentSelect.value, 10) : 1;
    const ratio = ratioSelect ? ratioSelect.value : '2:3';

    if (isRunning) {
      toast('已在运行中', 'warning');
      return;
    }

    isRunning = true;
    setStatus('connecting', '连接中');
    startBtn.disabled = true;

    if (pendingFallbackTimer) {
      clearTimeout(pendingFallbackTimer);
      pendingFallbackTimer = null;
    }

    let taskIds = [];
    try {
      taskIds = await createImagineTasks(prompt, ratio, concurrent, apiKey);
    } catch (e) {
      setStatus('error', '创建任务失败');
      startBtn.disabled = false;
      isRunning = false;
      return;
    }
    currentTaskIds = taskIds;

    if (modePreference === 'sse') {
      startSSE(taskIds);
      return;
    }

    connectionMode = 'ws';
    stopAllConnections();
    updateModeValue();

    let opened = 0;
    let fallbackDone = false;
    let fallbackTimer = null;
    if (modePreference === 'auto') {
      fallbackTimer = setTimeout(() => {
        if (!fallbackDone && opened === 0) {
          fallbackDone = true;
          startSSE(taskIds);
        }
      }, 1500);
    }
    pendingFallbackTimer = fallbackTimer;

    wsConnections = [];

    for (let i = 0; i < taskIds.length; i++) {
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const wsUrl = `${protocol}://${window.location.host}/api/v1/admin/imagine/ws?task_id=${encodeURIComponent(taskIds[i])}`;
      const ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        opened += 1;
        updateActive();
        if (i === 0) {
          setStatus('connected', '生成中');
          setButtons(true);
          toast(`已启动 ${concurrent} 个并发任务`, 'success');
        }
        sendStart(prompt, ws);
      };

      ws.onmessage = (event) => {
        handleMessage(event.data);
      };

      ws.onclose = () => {
        updateActive();
        if (connectionMode !== 'ws') {
          return;
        }
        const remaining = wsConnections.filter(w => w && w.readyState === WebSocket.OPEN).length;
        if (remaining === 0 && !fallbackDone) {
          setStatus('', '未连接');
          setButtons(false);
          isRunning = false;
          updateModeValue();
        }
      };

      ws.onerror = () => {
        updateActive();
        if (modePreference === 'auto' && opened === 0 && !fallbackDone) {
          fallbackDone = true;
          if (fallbackTimer) {
            clearTimeout(fallbackTimer);
          }
          startSSE(taskIds);
          return;
        }
        if (i === 0 && wsConnections.filter(w => w && w.readyState === WebSocket.OPEN).length === 0) {
          setStatus('error', '连接错误');
          startBtn.disabled = false;
          isRunning = false;
          updateModeValue();
        }
      };

      wsConnections.push(ws);
    }
  }

  function sendStart(promptOverride, targetWs) {
    const ws = targetWs || wsConnections[0];
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const prompt = promptOverride || (promptInput ? promptInput.value.trim() : '');
    const ratio = ratioSelect ? ratioSelect.value : '2:3';
    const dynamicRandom = dynamicRandomToggle ? dynamicRandomToggle.checked : false;
    const payload = {
      type: 'start',
      prompt,
      aspect_ratio: ratio,
      dynamic_random: dynamicRandom
    };
    ws.send(JSON.stringify(payload));
    updateError('');
  }

  async function stopConnection() {
    if (pendingFallbackTimer) {
      clearTimeout(pendingFallbackTimer);
      pendingFallbackTimer = null;
    }

    const apiKey = await ensureApiKey();
    if (apiKey && currentTaskIds.length > 0) {
      await stopImagineTasks(currentTaskIds, apiKey);
    }

    stopAllConnections();
    currentTaskIds = [];
    isRunning = false;
    updateActive();
    updateModeValue();
    setButtons(false);
    setStatus('', '未连接');
  }

  function clearImages() {
    if (waterfall) {
      waterfall.innerHTML = '';
    }
    imageCount = 0;
    filteredCount = 0;
    totalLatency = 0;
    latencyCount = 0;
    updateCount(imageCount);
    updateFiltered(filteredCount);
    updateLatency('');
    updateError('');
    if (emptyState) {
      emptyState.style.display = 'block';
    }
  }

  // === Imagine 模式切换 (NEW) ===
  function switchImagineMode(mode) {
    imagineMode = mode;
    imagineModeBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.imagineMode === mode));
    if (mode === 'edit') {
      if (editImageUpload) editImageUpload.classList.remove('hidden');
    } else {
      if (editImageUpload) editImageUpload.classList.add('hidden');
    }
  }

  if (imagineModeBtns.length > 0) {
    imagineModeBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.imagineMode;
        if (mode) switchImagineMode(mode);
      });
    });
  }

  // === 编辑模式图片上传 (NEW) ===
  function handleEditFile(file) {
    if (!file || !file.type.startsWith('image/')) {
      toast('请选择图片文件', 'error');
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      toast('图片不能超过 50MB', 'error');
      return;
    }
    editImageFile = file;
    const reader = new FileReader();
    reader.onload = (e) => {
      if (editPreviewImg) editPreviewImg.src = e.target.result;
      if (editUploadPlaceholder) editUploadPlaceholder.classList.add('hidden');
      if (editPreviewContainer) editPreviewContainer.classList.remove('hidden');
    };
    reader.readAsDataURL(file);
  }

  function removeEditImage() {
    editImageFile = null;
    if (editFileInput) editFileInput.value = '';
    if (editPreviewImg) editPreviewImg.src = '';
    if (editPreviewContainer) editPreviewContainer.classList.add('hidden');
    if (editUploadPlaceholder) editUploadPlaceholder.classList.remove('hidden');
  }

  if (editUploadArea) {
    editUploadArea.addEventListener('click', (e) => {
      if (e.target.closest('#editRemoveBtn')) return;
      if (editFileInput) editFileInput.click();
    });
    editUploadArea.addEventListener('dragover', (e) => { e.preventDefault(); editUploadArea.classList.add('dragover'); });
    editUploadArea.addEventListener('dragleave', () => { editUploadArea.classList.remove('dragover'); });
    editUploadArea.addEventListener('drop', (e) => {
      e.preventDefault();
      editUploadArea.classList.remove('dragover');
      const file = e.dataTransfer.files[0];
      if (file) handleEditFile(file);
    });
  }
  if (editFileInput) {
    editFileInput.addEventListener('change', () => {
      const file = editFileInput.files[0];
      if (file) handleEditFile(file);
    });
  }
  if (editRemoveBtn) {
    editRemoveBtn.addEventListener('click', (e) => { e.stopPropagation(); removeEditImage(); });
  }

  // === 编辑模式：通过 WebSocket 后端循环生成（复用文生图架构） ===
  async function startEditMode() {
    const prompt = promptInput ? promptInput.value.trim() : '';
    if (!prompt) {
      toast('请输入提示词', 'error');
      return;
    }
    if (!editImageFile) {
      toast('请上传参考图片', 'error');
      return;
    }

    const apiKey = await ensureApiKey();
    if (apiKey === null) {
      toast('请先登录后台', 'error');
      return;
    }

    if (isRunning) {
      toast('已在运行中', 'warning');
      return;
    }

    const concurrent = concurrentSelect ? parseInt(concurrentSelect.value, 10) : 1;
    const ratio = ratioSelect ? ratioSelect.value : '1:1';

    isRunning = true;
    setStatus('connecting', '上传参考图...');
    setButtons(true);

    // 第一步：上传参考图获取 image_urls
    let imageUrls = [];
    try {
      const formData = new FormData();
      formData.append('image', editImageFile);
      const uploadRes = await fetch('/v1/images/upload', {
        method: 'POST',
        headers: buildAuthHeaders(apiKey),
        body: formData,
      });
      if (!uploadRes.ok) {
        const errText = await uploadRes.text();
        throw new Error(errText || `上传失败 HTTP ${uploadRes.status}`);
      }
      const uploadData = await uploadRes.json();
      console.log('[Imagine Edit] uploadData:', JSON.stringify(uploadData));
      imageUrls = uploadData.image_urls || [];
      var editParentPostId = uploadData.parent_post_id || null;
      var editUploadToken = uploadData.upload_token || null;
      console.log('[Imagine Edit] parent_post_id:', editParentPostId, 'upload_token:', editUploadToken ? 'yes' : 'no');
      if (imageUrls.length === 0) {
        throw new Error('上传成功但未获取到图片 URL');
      }
    } catch (e) {
      setStatus('error', '上传失败');
      toast('参考图上传失败: ' + e.message, 'error');
      isRunning = false;
      setButtons(false);
      return;
    }

    // 第二步：创建 Imagine 任务 + WebSocket 连接
    setStatus('connecting', '建立连接...');
    let taskIds = [];
    try {
      taskIds = await createImagineTasks(prompt, ratio, concurrent, apiKey);
    } catch (e) {
      setStatus('error', '创建任务失败');
      toast('创建任务失败: ' + e.message, 'error');
      isRunning = false;
      setButtons(false);
      return;
    }
    currentTaskIds = taskIds;

    // 第三步：建立 WebSocket 连接并发送 start_edit 指令
    connectionMode = 'ws';
    stopAllConnections();
    updateModeValue();
    wsConnections = [];

    for (let i = 0; i < taskIds.length; i++) {
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const wsUrl = `${protocol}://${window.location.host}/api/v1/admin/imagine/ws?task_id=${encodeURIComponent(taskIds[i])}`;
      const ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        updateActive();
        if (i === 0) {
          setStatus('connected', '编辑生成中');
          toast(`已启动 ${concurrent} 个编辑任务`, 'success');
        }
        // 发送 start_edit 指令（而非 start）
        const dynamicRandom = dynamicRandomToggle ? dynamicRandomToggle.checked : false;
        const editPayload = {
          type: 'start_edit',
          prompt,
          aspect_ratio: ratio,
          image_urls: imageUrls,
          parent_post_id: editParentPostId,
          upload_token: editUploadToken,
          dynamic_random: dynamicRandom,
        };
        ws.send(JSON.stringify(editPayload));
        updateError('');
      };

      ws.onmessage = (event) => {
        handleMessage(event.data);
      };

      ws.onclose = () => {
        updateActive();
        const remaining = wsConnections.filter(w => w && w.readyState === WebSocket.OPEN).length;
        if (remaining === 0) {
          setStatus('', '未连接');
          setButtons(false);
          isRunning = false;
          updateModeValue();
        }
      };

      ws.onerror = () => {
        updateActive();
        if (i === 0 && wsConnections.filter(w => w && w.readyState === WebSocket.OPEN).length === 0) {
          setStatus('error', '连接错误');
          isRunning = false;
          setButtons(false);
          updateModeValue();
        }
      };

      wsConnections.push(ws);
    }
  }

  // === 原有的事件绑定（修改支持模式分发）===
  if (startBtn) {
    startBtn.addEventListener('click', () => {
      // 模式分发
      if (imagineMode === 'edit') {
        startEditMode();
      } else {
        startConnection();
      }
    });
  }

  if (stopBtn) {
    stopBtn.addEventListener('click', () => {
      stopConnection();
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', () => clearImages());
  }

  if (promptInput) {
    promptInput.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        if (imagineMode === 'edit') {
          startEditMode();
        } else {
          startConnection();
        }
      }
    });
  }

  if (ratioSelect) {
    ratioSelect.addEventListener('change', () => {
      if (isRunning) {
        if (connectionMode === 'sse') {
          stopConnection().then(() => {
            setTimeout(() => startConnection(), 50);
          });
          return;
        }
        wsConnections.forEach(ws => {
          if (ws && ws.readyState === WebSocket.OPEN) {
            sendStart(null, ws);
          }
        });
      }
    });
  }

  if (modeButtons.length > 0) {
    const saved = (() => {
      try {
        return localStorage.getItem(MODE_STORAGE_KEY);
      } catch (e) {
        return null;
      }
    })();
    if (saved) {
      setModePreference(saved, false);
    } else {
      setModePreference('auto', false);
    }

    modeButtons.forEach(btn => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.mode;
        if (!mode) return;
        setModePreference(mode);
        if (isRunning) {
          stopConnection().then(() => {
            setTimeout(() => startConnection(), 50);
          });
        }
      });
    });
  }

  // File System API support check
  if ('showDirectoryPicker' in window) {
    if (selectFolderBtn) {
      selectFolderBtn.disabled = false;
      selectFolderBtn.addEventListener('click', async () => {
        try {
          directoryHandle = await window.showDirectoryPicker({
            mode: 'readwrite'
          });
          useFileSystemAPI = true;
          if (folderPath) {
            folderPath.textContent = directoryHandle.name;
            selectFolderBtn.style.color = '#059669';
          }
          toast('已选择文件夹: ' + directoryHandle.name, 'success');
        } catch (e) {
          if (e.name !== 'AbortError') {
            toast('选择文件夹失败', 'error');
          }
        }
      });
    }
  }

  // Enable/disable folder selection based on auto-download
  if (autoDownloadToggle && selectFolderBtn) {
    autoDownloadToggle.addEventListener('change', () => {
      if (autoDownloadToggle.checked && 'showDirectoryPicker' in window) {
        selectFolderBtn.disabled = false;
      } else {
        selectFolderBtn.disabled = true;
      }
    });
  }

  // 过滤模糊图开关状态持久化
  if (filterBlurToggle) {
    try {
      const saved = localStorage.getItem(FILTER_BLUR_KEY);
      if (saved === 'true') filterBlurToggle.checked = true;
    } catch (e) { /* ignore */ }

    filterBlurToggle.addEventListener('change', () => {
      try {
        localStorage.setItem(FILTER_BLUR_KEY, filterBlurToggle.checked ? 'true' : 'false');
      } catch (e) { /* ignore */ }
    });
  }

  // Collapsible cards - 点击"连接状态"标题控制所有卡片
  const statusToggle = document.getElementById('statusToggle');

  if (statusToggle) {
    statusToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const cards = document.querySelectorAll('.imagine-card-collapsible');
      const allCollapsed = Array.from(cards).every(card => card.classList.contains('collapsed'));

      cards.forEach(card => {
        if (allCollapsed) {
          card.classList.remove('collapsed');
        } else {
          card.classList.add('collapsed');
        }
      });
    });
  }

  // Batch download functionality
  const batchDownloadBtn = document.getElementById('batchDownloadBtn');
  const selectionToolbar = document.getElementById('selectionToolbar');
  const toggleSelectAllBtn = document.getElementById('toggleSelectAllBtn');
  const downloadSelectedBtn = document.getElementById('downloadSelectedBtn');

  function enterSelectionMode() {
    isSelectionMode = true;
    selectedImages.clear();
    selectionToolbar.classList.remove('hidden');

    const items = document.querySelectorAll('.waterfall-item');
    items.forEach(item => {
      item.classList.add('selection-mode');
    });

    updateSelectedCount();
  }

  function exitSelectionMode() {
    isSelectionMode = false;
    selectedImages.clear();
    selectionToolbar.classList.add('hidden');

    const items = document.querySelectorAll('.waterfall-item');
    items.forEach(item => {
      item.classList.remove('selection-mode', 'selected');
    });
  }

  function toggleSelectionMode() {
    if (isSelectionMode) {
      exitSelectionMode();
    } else {
      enterSelectionMode();
    }
  }

  function toggleImageSelection(item) {
    if (!isSelectionMode) return;

    if (item.classList.contains('selected')) {
      item.classList.remove('selected');
      selectedImages.delete(item);
    } else {
      item.classList.add('selected');
      selectedImages.add(item);
    }

    updateSelectedCount();
  }

  function updateSelectedCount() {
    const countSpan = document.getElementById('selectedCount');
    if (countSpan) {
      countSpan.textContent = selectedImages.size;
    }
    if (downloadSelectedBtn) {
      downloadSelectedBtn.disabled = selectedImages.size === 0;
    }

    // Update toggle select all button text
    if (toggleSelectAllBtn) {
      const items = document.querySelectorAll('.waterfall-item');
      const allSelected = items.length > 0 && selectedImages.size === items.length;
      toggleSelectAllBtn.textContent = allSelected ? '取消全选' : '全选';
    }
  }

  function toggleSelectAll() {
    const items = document.querySelectorAll('.waterfall-item');
    const allSelected = items.length > 0 && selectedImages.size === items.length;

    if (allSelected) {
      // Deselect all
      items.forEach(item => {
        item.classList.remove('selected');
      });
      selectedImages.clear();
    } else {
      // Select all
      items.forEach(item => {
        item.classList.add('selected');
        selectedImages.add(item);
      });
    }

    updateSelectedCount();
  }

  async function downloadSelectedImages() {
    if (selectedImages.size === 0) {
      toast('请先选择要下载的图片', 'warning');
      return;
    }

    if (typeof JSZip === 'undefined') {
      toast('JSZip 库加载失败，请刷新页面重试', 'error');
      return;
    }

    toast(`正在打包 ${selectedImages.size} 张图片...`, 'info');
    downloadSelectedBtn.disabled = true;
    downloadSelectedBtn.textContent = '打包中...';

    const zip = new JSZip();
    const imgFolder = zip.folder('images');
    let processed = 0;

    try {
      for (const item of selectedImages) {
        const url = item.dataset.imageUrl;
        const prompt = item.dataset.prompt || 'image';

        try {
          let blob = null;
          if (url && url.startsWith('data:')) {
            blob = dataUrlToBlob(url);
          } else if (url) {
            const response = await fetch(url);
            blob = await response.blob();
          }
          if (!blob) {
            throw new Error('empty blob');
          }
          const filename = `${prompt.substring(0, 30).replace(/[^a-zA-Z0-9\u4e00-\u9fa5]/g, '_')}_${processed + 1}.png`;
          imgFolder.file(filename, blob);
          processed++;

          // Update progress
          downloadSelectedBtn.innerHTML = `打包中... (${processed}/${selectedImages.size})`;
        } catch (error) {
          console.error('Failed to fetch image:', error);
        }
      }

      if (processed === 0) {
        toast('没有成功获取任何图片', 'error');
        return;
      }

      // Generate zip file
      downloadSelectedBtn.textContent = '生成压缩包...';
      const content = await zip.generateAsync({ type: 'blob' });

      // Download zip
      const link = document.createElement('a');
      link.href = URL.createObjectURL(content);
      link.download = `imagine_${new Date().toISOString().slice(0, 10)}_${Date.now()}.zip`;
      link.click();
      URL.revokeObjectURL(link.href);

      toast(`成功打包 ${processed} 张图片`, 'success');
      exitSelectionMode();
    } catch (error) {
      console.error('Download failed:', error);
      toast('打包失败，请重试', 'error');
    } finally {
      downloadSelectedBtn.disabled = false;
      downloadSelectedBtn.innerHTML = `下载 <span id="selectedCount" class="selected-count">${selectedImages.size}</span>`;
    }
  }

  if (batchDownloadBtn) {
    batchDownloadBtn.addEventListener('click', toggleSelectionMode);
  }

  if (toggleSelectAllBtn) {
    toggleSelectAllBtn.addEventListener('click', toggleSelectAll);
  }

  if (downloadSelectedBtn) {
    downloadSelectedBtn.addEventListener('click', downloadSelectedImages);
  }


  // Handle image/checkbox clicks in waterfall
  if (waterfall) {
    waterfall.addEventListener('click', (e) => {
      const item = e.target.closest('.waterfall-item');
      if (!item) return;

      if (isSelectionMode) {
        // In selection mode, clicking anywhere on the item toggles selection
        toggleImageSelection(item);
      } else {
        // In normal mode, only clicking the image opens lightbox
        if (e.target.closest('.waterfall-item img')) {
          const img = e.target.closest('.waterfall-item img');
          const images = getAllImages();
          const index = images.indexOf(img);

          if (index !== -1) {
            updateLightbox(index);
            lightbox.classList.add('active');
          }
        }
      }
    });
  }

  // Lightbox for image preview with navigation
  const lightboxPrev = document.getElementById('lightboxPrev');
  const lightboxNext = document.getElementById('lightboxNext');
  let currentImageIndex = -1;

  function getAllImages() {
    return Array.from(document.querySelectorAll('.waterfall-item img'));
  }

  function updateLightbox(index) {
    const images = getAllImages();
    if (index < 0 || index >= images.length) return;

    currentImageIndex = index;
    lightboxImg.src = images[index].src;

    // Update navigation buttons state
    if (lightboxPrev) lightboxPrev.disabled = (index === 0);
    if (lightboxNext) lightboxNext.disabled = (index === images.length - 1);
  }

  function showPrevImage() {
    if (currentImageIndex > 0) {
      updateLightbox(currentImageIndex - 1);
    }
  }

  function showNextImage() {
    const images = getAllImages();
    if (currentImageIndex < images.length - 1) {
      updateLightbox(currentImageIndex + 1);
    }
  }

  if (lightbox && closeLightbox) {
    closeLightbox.addEventListener('click', (e) => {
      e.stopPropagation();
      lightbox.classList.remove('active');
      currentImageIndex = -1;
    });

    lightbox.addEventListener('click', () => {
      lightbox.classList.remove('active');
      currentImageIndex = -1;
    });

    // Prevent closing when clicking on the image
    if (lightboxImg) {
      lightboxImg.addEventListener('click', (e) => {
        e.stopPropagation();
      });
    }

    // Navigation buttons
    if (lightboxPrev) {
      lightboxPrev.addEventListener('click', (e) => {
        e.stopPropagation();
        showPrevImage();
      });
    }

    if (lightboxNext) {
      lightboxNext.addEventListener('click', (e) => {
        e.stopPropagation();
        showNextImage();
      });
    }

    // Keyboard navigation
    document.addEventListener('keydown', (e) => {
      if (!lightbox.classList.contains('active')) return;

      if (e.key === 'Escape') {
        lightbox.classList.remove('active');
        currentImageIndex = -1;
      } else if (e.key === 'ArrowLeft') {
        showPrevImage();
      } else if (e.key === 'ArrowRight') {
        showNextImage();
      }
    });
  }

  // Make floating actions draggable
  const floatingActions = document.getElementById('floatingActions');
  if (floatingActions) {
    let isDragging = false;
    let startX, startY, initialLeft, initialTop;

    floatingActions.style.touchAction = 'none';

    floatingActions.addEventListener('pointerdown', (e) => {
      if (e.target.tagName.toLowerCase() === 'button' || e.target.closest('button')) return;

      e.preventDefault();
      isDragging = true;
      floatingActions.setPointerCapture(e.pointerId);
      startX = e.clientX;
      startY = e.clientY;

      const rect = floatingActions.getBoundingClientRect();

      if (!floatingActions.style.left || floatingActions.style.left === '') {
        floatingActions.style.left = rect.left + 'px';
        floatingActions.style.top = rect.top + 'px';
        floatingActions.style.transform = 'none';
        floatingActions.style.bottom = 'auto';
      }

      initialLeft = parseFloat(floatingActions.style.left);
      initialTop = parseFloat(floatingActions.style.top);

      floatingActions.classList.add('shadow-xl');
    });

    document.addEventListener('pointermove', (e) => {
      if (!isDragging) return;

      const dx = e.clientX - startX;
      const dy = e.clientY - startY;

      floatingActions.style.left = `${initialLeft + dx}px`;
      floatingActions.style.top = `${initialTop + dy}px`;
    });

    document.addEventListener('pointerup', (e) => {
      if (isDragging) {
        isDragging = false;
        floatingActions.releasePointerCapture(e.pointerId);
        floatingActions.classList.remove('shadow-xl');
      }
    });
  }

  // === 词库标签中文映射表 ===
  const WILDCARD_CN_MAP = {
    'camera': '📷 镜头角度',
    'legwear': '🦵 腿部穿着',
    'nsfw_artist': '🎨 画师风格',
    'nsfw_hardcore': '🔞 硬核描写',
    'nsfw_pose': '💋 色情姿势',
    'nsfw_situation': '🛏️ 情色场景',
    'outfit': '👗 服装',
    'pose': '🧍 普通姿势',
    'quality': '✨ 画质增强',
    'style': '🖌️ 画风'
  };

  // === 动态随机：加载词库标签并支持点击展开下拉 ===
  // 词库内容缓存
  const _wildcardItemsCache = {};
  // 当前打开的下拉菜单
  let _activeDropdown = null;

  // 关闭所有下拉菜单
  function closeWildcardDropdown() {
    if (_activeDropdown) {
      _activeDropdown.remove();
      _activeDropdown = null;
    }
  }

  // 全局点击关闭
  document.addEventListener('click', (e) => {
    if (_activeDropdown && !e.target.closest('.wildcard-tag-wrapper')) {
      closeWildcardDropdown();
    }
  });

  // Esc 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeWildcardDropdown();
  });

  // 向输入框插入文本的通用方法
  function insertToPrompt(text) {
    if (!promptInput) return;
    const start = promptInput.selectionStart;
    const end = promptInput.selectionEnd;
    const val = promptInput.value;
    promptInput.value = val.substring(0, start) + text + val.substring(end);
    promptInput.selectionStart = promptInput.selectionEnd = start + text.length;
    promptInput.focus();
  }

  // 获取词库内容（带缓存）
  async function fetchWildcardItems(name) {
    if (_wildcardItemsCache[name]) return _wildcardItemsCache[name];
    try {
      const apiKey = await ensureApiKey();
      const res = await fetch(`/api/v1/admin/imagine/wildcards/${name}`, {
        headers: buildAuthHeaders(apiKey)
      });
      if (!res.ok) return null;
      const data = await res.json();
      _wildcardItemsCache[name] = data.items || [];
      return _wildcardItemsCache[name];
    } catch (e) {
      console.warn(`加载词库 ${name} 内容失败:`, e);
      return null;
    }
  }

  // 显示下拉菜单（fixed 定位，挂到 body 上，避免被 overflow:hidden 切割）
  async function showWildcardDropdown(name, btnElement) {
    // 如果已经打开了同一个菜单，关闭
    if (_activeDropdown && _activeDropdown.dataset.wildcardName === name) {
      closeWildcardDropdown();
      return;
    }
    closeWildcardDropdown();

    const items = await fetchWildcardItems(name);
    if (!items || items.length === 0) {
      insertToPrompt(`<${name}>`);
      return;
    }

    const cnLabel = WILDCARD_CN_MAP[name] || name;
    const dropdown = document.createElement('div');
    dropdown.className = 'wildcard-dropdown';
    dropdown.dataset.wildcardName = name;

    // 用 fixed 定位，根据按钮位置计算
    const rect = btnElement.getBoundingClientRect();
    dropdown.style.position = 'fixed';
    dropdown.style.top = (rect.bottom + 6) + 'px';
    dropdown.style.left = rect.left + 'px';

    // 第一项：随机
    const randomItem = document.createElement('button');
    randomItem.type = 'button';
    randomItem.className = 'wildcard-dropdown-item wildcard-dropdown-random';
    randomItem.textContent = `🎲 随机（插入 <${name}>）`;
    randomItem.addEventListener('click', (e) => {
      e.stopPropagation();
      insertToPrompt(`<${name}>`);
      closeWildcardDropdown();
    });
    dropdown.appendChild(randomItem);

    // 列出所有子Prompt（支持 {cn, en} 对象和纯字符串）
    items.forEach((item) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'wildcard-dropdown-item';

      if (typeof item === 'object' && item.cn && item.en) {
        // 中英文对照格式：显示中文，点击插入英文
        const cnSpan = document.createElement('span');
        cnSpan.className = 'dropdown-item-cn';
        cnSpan.textContent = item.cn;

        const enSpan = document.createElement('span');
        enSpan.className = 'dropdown-item-en';
        enSpan.textContent = item.en;

        btn.appendChild(cnSpan);
        btn.appendChild(enSpan);
        btn.title = item.en;

        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          insertToPrompt(item.en);
          closeWildcardDropdown();
        });
      } else {
        // 兼容旧格式：纯字符串
        const text = typeof item === 'string' ? item : JSON.stringify(item);
        btn.textContent = text;
        btn.title = text;
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          insertToPrompt(text);
          closeWildcardDropdown();
        });
      }

      dropdown.appendChild(btn);
    });

    // 挂到 body 上（不受父容器 overflow 影响）
    document.body.appendChild(dropdown);
    _activeDropdown = dropdown;

    // 检查是否超出视口底部，如果超出则改为向上弹出
    requestAnimationFrame(() => {
      const dropdownRect = dropdown.getBoundingClientRect();
      if (dropdownRect.bottom > window.innerHeight) {
        dropdown.style.top = (rect.top - dropdownRect.height - 6) + 'px';
      }
      // 检查是否超出右边界
      if (dropdownRect.right > window.innerWidth) {
        dropdown.style.left = (window.innerWidth - dropdownRect.width - 12) + 'px';
      }
    });
  }

  async function loadWildcardTags() {
    if (!wildcardTagsContainer) return;
    try {
      const apiKey = await ensureApiKey();
      const res = await fetch('/api/v1/admin/imagine/wildcards', {
        headers: buildAuthHeaders(apiKey)
      });
      if (!res.ok) return;
      const data = await res.json();
      const wildcards = data.wildcards || [];
      if (wildcards.length === 0) return;

      wildcardTagsContainer.innerHTML = '';
      wildcards.forEach(name => {
        // 外层容器（用于定位下拉菜单）
        const wrapper = document.createElement('div');
        wrapper.className = 'wildcard-tag-wrapper';

        const tag = document.createElement('button');
        tag.type = 'button';
        tag.className = 'wildcard-tag';
        const cnLabel = WILDCARD_CN_MAP[name] || name;
        tag.textContent = cnLabel;
        tag.title = `点击展开 ${cnLabel} 词库`;
        tag.addEventListener('click', (e) => {
          e.stopPropagation();
          showWildcardDropdown(name, tag);
        });

        wrapper.appendChild(tag);
        wildcardTagsContainer.appendChild(wrapper);
      });
    } catch (e) {
      console.warn('加载词库标签失败:', e);
    }
  }

  // 页面加载后延迟加载词库标签
  setTimeout(loadWildcardTags, 500);

})();
