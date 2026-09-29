document.addEventListener('click',function(e){
  var o=e.target.closest('[data-open]');
  if(o){var d=document.getElementById(o.getAttribute('data-open'));if(d&&d.showModal)d.showModal();}
  var c=e.target.closest('[data-close]');
  if(c){var p=c.closest('dialog');if(p)p.close();}
  if(e.target.tagName==='DIALOG')e.target.close();
});
