// Only the local planner may ask this extension to open a Wolt cart.
if (['http://127.0.0.1:8000','http://localhost:8000'].includes(location.origin)) {
  const reply = data => window.postMessage({channel:'wolt-cart-helper',...data},location.origin);
  window.addEventListener('message',async event => {
    if (event.source!==window || event.origin!==location.origin || event.data?.channel!=='wolt-planner') return;
    const data=event.data;
    if (data.type==='ping') { reply({type:'ready'}); return; }
    if (data.type!=='prepare' || typeof data.requestId!=='string') return;
    try {
      const result=await chrome.runtime.sendMessage({type:'PREPARE',restaurant:data.restaurant,handoffKey:data.handoffKey});
      reply({type:'prepared',requestId:data.requestId,...result});
    } catch {
      reply({type:'prepared',requestId:data.requestId,ok:false,error:'The helper connection was interrupted. Check Wolt before trying again.'});
    }
  });
  reply({type:'ready'});
}
