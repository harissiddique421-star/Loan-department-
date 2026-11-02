document.addEventListener("click", function (e) {
  var t = e.target.closest("[data-print]");
  if (t) { e.preventDefault(); window.print(); }
  var c = e.target.closest("[data-confirm]");
  if (c && !window.confirm(c.getAttribute("data-confirm"))) { e.preventDefault(); }
});
// Live preview for the applicant photo picker.
document.addEventListener("change", function (e) {
  var input = e.target;
  if (!input.matches("[data-preview]") || !input.files || !input.files[0]) return;
  var img = document.getElementById(input.getAttribute("data-preview"));
  if (!img) return;
  img.src = URL.createObjectURL(input.files[0]);
  img.classList.remove("empty");
});
