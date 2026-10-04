importScripts('rules.js');
const R=WoltCartRules;
let sequence=Promise.resolve();
const expires = job => Date.now()-job.createdAt>60*60*1000;
async function jobs() {
  const data=(await chrome.storage.session.get('jobs')).jobs || {};
  return Object.fromEntries(Object.entries(data).filter(([,job])=>!expires(job)).slice(-20));
}
async function handle(message,sender) {
  if (message.type==='PREPARE') {
    if (!R.appOrigin(sender.tab?.url) || sender.frameId!==0) throw Error('Open the local meal planner to prepare an order.');
    if (typeof message.handoffKey!=='string' || message.handoffKey.length>120 || !/^[a-zA-Z0-9:-]+$/.test(message.handoffKey)) throw Error('Invalid handoff');
    const restaurant=R.validateRestaurant(message.restaurant);
    const saved=await jobs();
    const existing=Object.values(saved).find(job=>job.handoffKey===message.handoffKey);
    if (existing) {
      try { await chrome.tabs.update(existing.tabId,{active:true}); }
      catch { throw Error('This selection was already opened. Check your Wolt cart or orders before preparing it again.'); }
      return {ok:true,restaurantId:restaurant.restaurant_id};
    }
    if (Object.values(saved).some(job=>['pending','preparing'].includes(job.status))) throw Error('Another cart is being prepared. Finish or stop it in the Wolt tab first.');
    const tab=await chrome.tabs.create({url:'about:blank',active:true});
    saved[tab.id]={tabId:tab.id,handoffKey:message.handoffKey,restaurant,createdAt:Date.now(),status:'pending',added:0,attempted:false};
    await chrome.storage.session.set({jobs:saved});
    try { await chrome.tabs.update(tab.id,{url:restaurant.url}); }
    catch (error) {
      saved[tab.id].status='needs_attention';
      await chrome.storage.session.set({jobs:saved});
      throw error;
    }
    return {ok:true,restaurantId:restaurant.restaurant_id};
  }
  let url;
  try { url=new URL(sender.tab?.url); } catch { return null; }
  if (url.origin!=='https://wolt.com' || sender.frameId!==0) return null;
  const saved=await jobs();
  const job=saved[sender.tab.id];
  if (!job) return null;
  if (message.type==='RETRY') {
    if (!['stopped','needs_attention'].includes(job.status) || job.added!==0 || job.attempted!==false)
      throw Error('An addition may already have happened. Check the Wolt cart; automatic replay is disabled.');
    if (R.venueKey(url.href)!==R.venueKey(job.restaurant.url)) throw Error('Return to the restaurant first.');
    if (Object.values(saved).some(other=>other.tabId!==job.tabId && ['pending','preparing'].includes(other.status)))
      throw Error('Another cart is being prepared.');
    job.status='pending'; job.message='';
    await chrome.storage.session.set({jobs:saved});
    return {ok:true};
  }
  if (message.type==='CLAIM') {
    if (job.status!=='pending') return {...job,canRun:false};
    if (R.venueKey(url.href)!==R.venueKey(job.restaurant.url)) return null;
    job.status='preparing';
    await chrome.storage.session.set({jobs:saved});
    return {...job,canRun:true};
  }
  if (message.type==='UPDATE' && ['preparing','needs_attention','review_ready','stopped'].includes(message.status)) {
    job.status=message.status;
    if (message.attempted===true) job.attempted=true;
    if (Number.isInteger(message.added) && message.added>=job.added && message.added<=job.restaurant.meals.length) job.added=message.added;
    job.message=String(message.message || '').slice(0,300);
    await chrome.storage.session.set({jobs:saved});
    return {ok:true};
  }
  return null;
}
chrome.runtime.onMessage.addListener((message,sender,sendResponse)=>{
  sequence=sequence.catch(()=>{}).then(()=>handle(message,sender));
  sequence.then(sendResponse,error=>sendResponse({ok:false,error:error.message}));
  return true;
});
chrome.tabs.onRemoved.addListener(tabId=>{
  sequence=sequence.catch(()=>{}).then(async()=>{
    const saved=await jobs();
    if (saved[tabId]) { saved[tabId].status='stopped'; await chrome.storage.session.set({jobs:saved}); }
  });
});
