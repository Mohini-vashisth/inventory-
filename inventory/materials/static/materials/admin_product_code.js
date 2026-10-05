// Admin "Add/Change product code": fill Item Code from the product type, grade and
// size as soon as all three are chosen. The format lives in Python (product_codes.py);
// this just asks the lookup endpoint. A code typed by hand is never overwritten.
(function () {
  var category = document.getElementById('id_category');
  var grade = document.getElementById('id_grade');
  var size = document.getElementById('id_size');
  var code = document.getElementById('id_item_code');
  if (!category || !grade || !size || !code || !code.dataset.lookupUrl) return;

  var autoFilled = false;
  var timer = null;
  var hint = document.createElement('div');
  hint.style.cssText = 'margin-top:4px;font-size:12px;color:#4e73df;';
  code.parentNode.appendChild(hint);

  // Same rule as Python's grade_key: case, spaces and punctuation don't make a different grade.
  function gradeKey(value) { return (value || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }

  function canonicalGrade() {
    var typed = gradeKey(grade.value);
    var listed = !typed ? null : Array.prototype.find.call(document.querySelectorAll('#grade-options option'), function (option) {
      return gradeKey(option.value) === typed;
    });
    if (listed && listed.value !== grade.value) grade.value = listed.value;
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
    if (!category.value || !grade.value.trim() || !size.value) { clearAuto(); hint.textContent = ''; return; }
    timer = setTimeout(function () {
      var query = '?category=' + encodeURIComponent(category.value) +
        '&grade=' + encodeURIComponent(grade.value.trim()) + '&size=' + encodeURIComponent(size.value);
      fetch(code.dataset.lookupUrl + query, {credentials: 'same-origin'})
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (code.value && !autoFilled) return;
          if (data.exists) {
            clearAuto();
            hint.style.color = '#b45309';
            hint.textContent = 'A product code for this type, grade and size already exists: ' + data.item_code;
          } else if (data.item_code) {
            code.value = data.item_code;
            autoFilled = true;
            hint.style.color = '#4e73df';
            hint.textContent = 'Generated from the product type, grade and size.';
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
  [category, grade, size].forEach(function (field) {
    field.addEventListener('input', update);
    field.addEventListener('change', update);
  });
  update();
})();
