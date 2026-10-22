// Admin pages that have a product type, a grade and a product code together (a query's products, an order):
// as soon as the type and grade are chosen, pick the catalogue's product code if there is one. A code chosen by
// hand is left alone. The match itself is done by the same lookup endpoint the quote form uses.
(function () {
  var URL = '/product-codes/lookup/';
  function wire(scope) {
    var type = scope.querySelector('[name$="product_type"]');
    var grade = scope.querySelector('[name$="grade"]');
    var code = scope.querySelector('select[name$="product_code"]');
    if (!type || !grade || !code || code.dataset.followWired) return;
    code.dataset.followWired = '1';
    var byHand = false, timer = null;
    code.addEventListener('change', function () { byHand = true; });
    function follow() {
      clearTimeout(timer);
      if (byHand || !type.value || !grade.value.trim()) return;
      timer = setTimeout(function () {
        fetch(URL + '?product_type=' + encodeURIComponent(type.value) + '&grade=' + encodeURIComponent(grade.value.trim()),
              {credentials: 'same-origin'})
          .then(function (r) { return r.json(); })
          .then(function (data) { if (!byHand && data.exists && data.id) code.value = String(data.id); });
      }, 300);
    }
    type.addEventListener('change', follow);
    grade.addEventListener('input', follow);
    grade.addEventListener('change', follow);
  }
  function wireAll() {
    var rows = document.querySelectorAll('tr.form-row, .inline-related');
    if (rows.length) rows.forEach(wire); else wire(document);
  }
  document.addEventListener('DOMContentLoaded', wireAll);
  document.addEventListener('formset:added', function (event) { wire(event.target); });
})();
