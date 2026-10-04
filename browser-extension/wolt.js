/* Runs only in an explicitly opened helper tab. Uses the visible UI, never private APIs. */
(async()=>{
  const job=await chrome.runtime.sendMessage({type:'CLAIM'});
  if (!job?.restaurant) return;
  const R=WoltCartRules;
  let stopped=false;
  let finished=false;
  let added=job.added || 0;
  let attempted=job.attempted !== false;
  let pendingAddition=false;
  let phase='Starting preparation';
  const host=document.createElement('aside');
  host.style.cssText='position:fixed;right:16px;bottom:16px;z-index:2147483647;max-width:360px;width:calc(100% - 32px)';
  document.documentElement.append(host);
  const shadow=host.attachShadow({mode:'closed'});
  shadow.innerHTML='<style>:host{all:initial;font:14px/1.5 system-ui;color:#17231f;text-align:left}[hidden]{display:none!important}.panel{background:#fffef8;border:2px solid #163f32;border-radius:16px;padding:18px;box-shadow:0 8px 35px #0003}h2{font-size:17px;margin:0 0 10px}p{line-height:1.5;margin:8px 0}button{all:initial;box-sizing:border-box;display:inline-block;font:600 13px/1.4 system-ui;color:#17231f;padding:10px 12px;margin:8px 8px 0 0;border:1px solid #7a857d;border-radius:8px;background:#fff;cursor:pointer}button:disabled{opacity:.5;cursor:default}button:focus-visible{outline:3px solid #163f32}ul{font-size:12px;line-height:1.5;padding-left:18px}</style><div class="panel"><h2>Wolt Cart Helper · 0.1.8</h2><p id="status" role="status"></p><ul id="meals"></ul><p><small>You review delivery details and pay. This helper never places or pays for an order.</small></p><button type="button" id="stop">Stop preparation</button><button type="button" id="retry" hidden>Retry automatic preparation</button></div>';
  const status=shadow.querySelector('#status');
  const list=shadow.querySelector('#meals');
  for (const meal of job.restaurant.meals) { const li=document.createElement('li');li.textContent=`1 × ${meal.name} · ${meal.price_mkd} MKD`;list.append(li); }
  async function update(state,message) {
    status.textContent=message;
    const reply=await chrome.runtime.sendMessage({type:'UPDATE',status:state,added,attempted,message});
    if (!reply?.ok) throw Error(reply?.error || 'Helper connection lost.');
  }
  function progress() {
    return pendingAddition ? `${added} item(s) confirmed added. An unconfirmed addition may also be in the cart; check Wolt before retrying.` : added ? `${added} item(s) confirmed added. Check your cart before preparing again.` : 'No Add action was attempted; the helper added nothing.';
  }
  function showRetry() { shadow.querySelector('#retry').hidden=attempted || added!==0; }
  shadow.querySelector('#retry').addEventListener('click',async()=>{
    shadow.querySelector('#retry').disabled=true;
    try {
      const reply=await chrome.runtime.sendMessage({type:'RETRY'});
      if (!reply?.ok) throw Error(reply?.error || 'Cannot retry automatically.');
      location.reload();
    } catch(error) { status.textContent=error.message; shadow.querySelector('#retry').disabled=false; }
  });
  shadow.querySelector('#stop').addEventListener('click',event=>{
    const button=shadow.querySelector('#stop');
    if (finished || stopped || button.hidden || button.disabled || event.currentTarget!==button) return;
    stopped=true;
    update('stopped',`Stopped during: ${phase}. ${progress()}`).then(showRetry).catch(error=>{status.textContent=error.message;});
    shadow.querySelector('#stop').disabled=true;
  });
  if (!job.canRun) {
    finished=true;
    const message=job.message || 'This preparation was interrupted. Check your cart manually; the helper will not repeat additions.';
    if (job.status==='preparing') await update('needs_attention','Preparation was interrupted. Check your cart manually; additions will not be repeated.');
    else status.textContent=message;
    shadow.querySelector('#stop').hidden=true;
    showRetry();
    return;
  }
  const visible=element=>Boolean(element?.getClientRects().length) && getComputedStyle(element).visibility!=='hidden';
  const label=element=>R.normalize(element.innerText || element.getAttribute('aria-label') || element.textContent);
  const controls=(scope=document)=>[...scope.querySelectorAll('button,a,[role="button"]')].filter(visible);
  function check() {
    if (stopped) throw Error('Preparation stopped. Review any items already added.');
    if (R.venueKey(location.href)!==R.venueKey(job.restaurant.url)) throw Error('The restaurant page changed. Review your cart manually.');
  }
  async function waitFor(find,message,timeout=12000) {
    const end=Date.now()+timeout;
    while (Date.now()<end) {
      check();
      const value=find();
      if (value) return value;
      await new Promise(resolve=>setTimeout(resolve,200));
    }
    throw Error(typeof message==='function' ? message() : message);
  }
  function cartControl(allowEmpty=false) {
    const states=[];
    let unknownControl=false;
    for (const button of controls()) {
      // Some layouts put quantity in the accessible name, others in text.
      const labels=[label(button),R.normalize(button.getAttribute('aria-label'))];
      if (!labels.some(value=>/\bView (order|cart)\b/i.test(value))) continue;
      const parsed=labels.map(R.cart).filter(Boolean);
      if (!parsed.length && labels.some(value=>R.prices(value).some(price=>price!==0))) unknownControl=true;
      for (const state of parsed) states.push({button,...state});
    }
    const empty=allowEmpty && [...document.querySelectorAll('p,h2,h3,h4,span,div,[role="status"]')]
      .some(node=>visible(node) && R.emptyCart(node.textContent));
    if (states.length) {
      if (states.some(state=>state.count!==states[0].count || state.total!==states[0].total)) return null;
      if (empty && (states[0].count!==0 || states[0].total!==0)) return null;
      return states[0];
    }
    // Explicit visible empty-state text is evidence; a missing button is not.
    if (empty && !unknownControl) return {button:null,count:0,total:0};
    return null;
  }
  function cartDiagnostic() {
    const labels=controls().map(label).filter(value=>/\b(?:cart|basket|view order)\b/i.test(value));
    return labels.length ? `Cart controls found: ${labels.slice(0,3).map(value=>value.slice(0,100)).join(' | ')}` : 'No readable cart control or explicit empty-cart message was found.';
  }
  async function initialCart() {
    const direct=cartControl(true);
    if (direct) return direct;
    const ready=await waitFor(()=>{
      const cart=cartControl(true);
      if (cart) return {cart};
      const orders=controls().filter(button=>label(button)==='Your orders');
      return orders.length===1 ? {orders} : null;
    },()=>`Cart controls did not load. ${cartDiagnostic()}`,20000);
    if (ready.cart) return ready.cart;
    const orders=ready.orders;
    if ([...document.querySelectorAll('[role="dialog"],dialog[open]')].some(visible))
      throw Error('Close the open Wolt dialog, then retry preparation.');
    orders[0].click();
    const drawer=await waitFor(()=>[...document.querySelectorAll('[data-test-id="visible-baskets.modal"]')].find(visible),'The Your orders drawer did not open.');
    const empty=await waitFor(()=>[...drawer.querySelectorAll('p,h2,h3,h4,span,div')].some(node=>visible(node) && R.emptyCart(node.textContent))
      || /Item subtotal:/.test(drawer.innerText) && 'occupied','The shopping-cart list did not finish loading.');
    if (empty==='occupied') throw Error('Wolt has an existing shopping cart. Review it in Your orders before preparing another cart.');
    const close=controls(drawer).filter(button=>label(button)==='Close the dialog');
    if (close.length!==1) throw Error('Could not close the cart drawer.');
    close[0].click();
    await waitFor(()=>!visible(drawer),'The cart drawer did not close.');
    return {count:0,total:0,button:null};
  }
  function itemCard(meal) {
    const headings=[...document.querySelectorAll('h2,h3,h4,[role="heading"]')].filter(element=>visible(element) && R.normalize(element.textContent)===R.normalize(meal.name));
    const matches=[];
    for (const heading of headings) {
      const ancestor=heading.closest('button,a,[role="button"]');
      let scope=heading.parentElement;
      for (let depth=0;scope && depth<5;depth++,scope=scope.parentElement) {
        if (scope.querySelectorAll('h2,h3,h4,[role="heading"]').length>1) break;
        const priceFields=[...scope.querySelectorAll('[data-test-id="horizontal-item-card-price"]')];
        const prices=R.prices(priceFields.length ? priceFields.map(node=>node.textContent).join(' ') : scope.innerText);
        if (prices.length!==1 || prices[0]!==meal.price_mkd) continue;
        const buttons=ancestor ? [ancestor] : [...scope.querySelectorAll('button[data-test-id="horizontal-item-card-button"],button[data-test-id="CardLinkButton"]')];
        const clickable=buttons.filter(visible);
        if (clickable.length===1) { matches.push({button:clickable[0],signature:JSON.stringify([R.normalize(heading.textContent),[...heading.parentElement.querySelectorAll('p')].map(node=>R.normalize(node.textContent)),prices[0]])}); break; }
      }
    }
    const unique=[...new Map(matches.map(match=>[match.button,match])).values()];
    // Wolt repeats dishes in Most ordered and their menu category, sometimes
    // using different item IDs. Compare dish descriptions, not promotional badges in the card.
    if (!unique.length || new Set(unique.map(match=>match.signature)).size!==1) throw Error(`Cannot uniquely match “${meal.name}” at ${meal.price_mkd} MKD.`);
    return unique[0].button;
  }
  function optionsReady(dialog) {
    const fields=[...dialog.querySelectorAll('input[type="radio"],input[type="checkbox"],select,[role="radio"],[role="checkbox"],[role="combobox"]')];
    if (!fields.length) return true;
    // Preserve Wolt's existing radio defaults; never select an alternative.
    // Other option controls still need explicit user selection/review.
    if (fields.some(input=>input.tagName!=='INPUT' || input.type!=='radio')) return false;
    const groups=new Map();
    for (const input of fields) {
      if (!input.name) return false;
      if (!groups.has(input.name)) groups.set(input.name,[]);
      groups.get(input.name).push(input);
    }
    return [...groups.values()].every(group=>group.filter(input=>input.checked && !input.disabled).length===1);
  }
  try {
    phase='checking the restaurant';
    await update('preparing','Checking the restaurant and your existing cart…');
    await waitFor(()=>[...document.querySelectorAll('h1')].find(node=>visible(node) && R.normalize(node.textContent)===R.normalize(job.restaurant.restaurant_name)), 'Restaurant not ready. Sign in, confirm your delivery address, or check restaurant availability in Wolt.');
    phase='checking the cart';
    let cart=await initialCart();
    if (!cart) cart=await waitFor(()=>cartControl(true),()=>`Cart detection paused. ${cartDiagnostic()} No meals were added.`,20000);
    if (cart.count!==0 || cart.total!==0) throw Error('Your Wolt cart already contains items. The helper will not replace or merge it. Finish or clear it yourself first.');
    if ([...document.querySelectorAll('[role="dialog"],dialog[open]')].some(visible)) throw Error('Close the open Wolt dialog and review the selection manually.');
    let expectedTotal=0;
    for (const meal of job.restaurant.meals) {
      check();
      phase=`finding ${meal.name}`;
      await update('preparing',`Finding ${meal.name}…`);
      const card=await waitFor(()=>{try{return itemCard(meal);}catch{return null;}},`Could not match ${meal.name} at the planned price. Check that this item is available.`);
      card.click();
      const dialog=await waitFor(()=>[...document.querySelectorAll('[role="dialog"],dialog[open]')].find(element=>visible(element) && [...element.querySelectorAll('h1,h2,h3,[role="heading"]')].some(heading=>R.normalize(heading.textContent)===R.normalize(meal.name))), `Open “${meal.name}” manually; its item dialog could not be verified.`);
      if (!optionsReady(dialog)) {
        phase=`waiting for your choices for ${meal.name}`;
        // Persist a conservative no-replay state before the user can add.
        attempted=true;
        let confirmedPrice=null;
        const capture=event=>{
          const button=controls(dialog).find(button=>button===event.target || button.contains?.(event.target));
          if (!button || !R.canAdd(label(button)) || button.disabled) return;
          const prices=R.prices(label(button));
          if (prices.length===1 && prices[0]>0) confirmedPrice=prices[0];
          pendingAddition=true;
        };
        dialog.addEventListener('click',capture,true);
        try {
          await update('preparing',`Choose any extras for “${meal.name}”, then click Wolt’s Add to order button. I’ll continue with the remaining meals after the cart confirms it. Extras increase your total.`);
          await waitFor(()=>{
            const current=cartControl();
            return confirmedPrice!==null && current && current.count===added+1 && current.total===expectedTotal+confirmedPrice;
          },'Your item addition could not be confirmed. Check the cart before continuing manually.',300000);
          expectedTotal+=confirmedPrice;
          added++;
          pendingAddition=false;
          await update('preparing',`${added} of ${job.restaurant.meals.length} items added.`);
        } finally { dialog.removeEventListener('click',capture,true); }
        continue;
      }
      const quantities=[...dialog.querySelectorAll('input[type="number"],[role="spinbutton"]')].filter(visible);
      if (quantities.some(input=>Number(input.value || input.getAttribute('aria-valuenow'))!==1)) throw Error('The item quantity is not one. Review it manually.');
      const buttons=controls(dialog).filter(button=>R.canAdd(label(button)) && !button.disabled && button.getAttribute('aria-disabled')!=='true');
      if (buttons.length!==1) throw Error('The Add to order button is unavailable or ambiguous. Complete this item manually.');
      const prices=R.prices(label(buttons[0])).length ? R.prices(label(buttons[0])) : R.prices(dialog.innerText);
      if (prices.length!==1 || prices[0]!==meal.price_mkd) throw Error(`The price of “${meal.name}” changed or cannot be verified. Review it in Wolt.`);
      check();
      // Single click only. Uncertain outcomes never cause an automatic retry.
      phase=`adding ${meal.name}`;
      attempted=true;
      pendingAddition=true;
      await update('preparing',`Adding ${meal.name}. If interrupted, check the cart before doing anything again.`);
      check();
      buttons[0].click();
      expectedTotal+=meal.price_mkd;
      await waitFor(()=>{ const current=cartControl();return current && current.count===added+1 && current.total===expectedTotal; },'The cart update could not be confirmed. Check what was added; the helper will not retry.');
      added++;
      pendingAddition=false;
      await update('preparing',`${added} of ${job.restaurant.meals.length} items added.`);
    }
    check();
    cart=cartControl();
    if (!cart || cart.count!==added || cart.total!==expectedTotal) throw Error('The cart changed. Review it before checkout.');
    cart.button.click();
    finished=true;
    shadow.querySelector('#stop').disabled=true;
    shadow.querySelector('#stop').hidden=true;
    await update('review_ready','Meals added. Review your cart, delivery address, time and fees in Wolt, then pay yourself.');
    // Only an explicitly named navigation control is allowed; never Pay/Place order.
    const end=Date.now()+4000;
    while (Date.now()<end && !stopped) {
      const checkout=controls().filter(button=>/^Go to checkout$/i.test(label(button)) && !button.disabled);
      if (checkout.length===1) { checkout[0].click(); break; }
      await new Promise(resolve=>setTimeout(resolve,200));
    }
    shadow.querySelector('#stop').hidden=true;
  } catch (error) {
    finished=true;
    shadow.querySelector('#stop').disabled=true;
    shadow.querySelector('#stop').hidden=true;
    if (!stopped) await update('needs_attention',`${error.message} ${progress()} No payment was made.`);
    showRetry();
    shadow.querySelector('#stop').hidden=true;
  }
})().catch(()=>{});
