// Admin "Add/Change product code": fill Item Code from the product type and grade
// as soon as both are chosen (size plays no part in a code). The format lives in Python (product_codes.py);
// this just asks the lookup endpoint. A code typed by hand is never overwritten.
(function () {
  var category = document.getElementById('id_category');
  var grade = document.getElementById('id_grade');
  var code = document.getElementById('id_item_code');
  if (!category || !grade || !code || !code.dataset.lookupUrl) return;

  var autoFilled = false;
  var timer = null;
  var hint = document.createElement('div');
  hint.style.cssText = 'margin-top:4px;font-size:12px;color:#2f7d5b;';
  code.parentNode.appendChild(hint);

  // Same rule as Python's grade_key: case, spaces and punctuation don't make a different grade.
  function gradeKey(value) { return (value || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }

  // Grades are stored as capital letters and digits only (models.normalize_grade): "en-8d" -> "EN8D".
  function canonicalGrade() {
    var normalized = grade.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (normalized !== grade.value) grade.value = normalized;
  }

  function clearAuto() {
    if (autoFilled) {
      code.value = '';
      autoFilled = false;
    }
  }

  function update() {
    clearTimeout(timer);
    if (code.value && !autoFilled) { hint.textContent = ''; return; }
    if (!category.value || !grade.value.trim()) { clearAuto(); hint.textContent = ''; return; }
    timer = setTimeout(function () {
      var query = '?category=' + encodeURIComponent(category.value) +
        '&grade=' + encodeURIComponent(grade.value.trim());
      fetch(code.dataset.lookupUrl + query, {credentials: 'same-origin'})
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (code.value && !autoFilled) return;
          if (data.exists) {
            clearAuto();
            hint.style.color = '#b45309';
            hint.textContent = 'A product code for this type and grade already exists: ' + data.item_code;
          } else if (data.item_code) {
            code.value = data.item_code;
            autoFilled = true;
            hint.style.color = '#2f7d5b';
            hint.textContent = 'Generated from the product type and grade.';
          } else {
            clearAuto();
            hint.style.color = '#b45309';
            hint.textContent = data.reason || '';
          }
        })
        .catch(function () { hint.textContent = ''; });
    }, 250);
  }

  code.addEventListener('input', function () { autoFilled = false; hint.textContent = ''; });
  grade.addEventListener('change', function () { canonicalGrade(); update(); });
  [category, grade].forEach(function (field) {
    field.addEventListener('input', update);
    field.addEventListener('change', update);
  });
  update();
})();
