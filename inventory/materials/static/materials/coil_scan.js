// Camera QR scanning for any page with the standard scan markup:
// #camera-scan-btn, #scan-modal, #scan-video, #scan-modal-status,
// #scan-modal-cancel, #coil-no-input, #coil-scan-form (id may vary —
// see below). Requires jsQR.min.js loaded first as a fallback for
// browsers without the native BarcodeDetector API.
(function () {
  var scanBtn = document.getElementById('camera-scan-btn');
  if (!scanBtn) return;

  var modal = document.getElementById('scan-modal');
  var video = document.getElementById('scan-video');
  var status = document.getElementById('scan-modal-status');
  var cancelBtn = document.getElementById('scan-modal-cancel');
  var coilInput = document.getElementById('coil-no-input');
  var form = document.getElementById('coil-scan-form');

  var stream = null;
  var rafId = null;
  var canvas = document.createElement('canvas');
  var ctx = canvas.getContext('2d', { willReadFrequently: true });

  function stopScanning() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = null;
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
    modal.hidden = true;
  }

  function onDecoded(text) {
    stopScanning();
    coilInput.value = text;
    form.submit();
  }

  // Native path: Chrome/Samsung Internet on Android support this directly,
  // no extra library needed.
  function scanWithBarcodeDetector(detector) {
    detector.detect(video).then(function (codes) {
      if (codes.length > 0) {
        onDecoded(codes[0].rawValue);
      } else {
        rafId = requestAnimationFrame(function () { scanWithBarcodeDetector(detector); });
      }
    }).catch(function () {
      rafId = requestAnimationFrame(function () { scanWithBarcodeDetector(detector); });
    });
  }

  // Fallback path: draw each frame to a canvas and decode with jsQR.
  function scanWithJsQR() {
    if (video.readyState === video.HAVE_ENOUGH_DATA) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      var imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
      var code = window.jsQR && window.jsQR(imageData.data, imageData.width, imageData.height);
      if (code) {
        onDecoded(code.data);
        return;
      }
    }
    rafId = requestAnimationFrame(scanWithJsQR);
  }

  scanBtn.addEventListener('click', function () {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      status.textContent = 'Camera access isn\'t available in this browser — type the number instead.';
      modal.hidden = false;
      return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
      .then(function (s) {
        stream = s;
        video.srcObject = stream;
        modal.hidden = false;
        video.play();
        if (window.BarcodeDetector) {
          var detector = new BarcodeDetector({ formats: ['qr_code'] });
          scanWithBarcodeDetector(detector);
        } else {
          scanWithJsQR();
        }
      })
      .catch(function () {
        status.textContent = 'Camera access was denied — type the number instead.';
        modal.hidden = false;
      });
  });

  cancelBtn.addEventListener('click', stopScanning);
})();
