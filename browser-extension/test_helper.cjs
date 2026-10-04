const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const R=require('./rules.js');
assert.equal(R.appOrigin('http://localhost:8000/'),true);
assert.equal(R.appOrigin('http://localhost:9000/'),false);
assert.equal(R.appOrigin('https://evil.test/'),false);
for(const text of ['Pay','Place order','Confirm purchase','Add to order and pay','Pay MKD 300']) assert.equal(R.canAdd(text),false);
assert.equal(R.canAdd('Add to order · MKD 300'),true);
assert.equal(R.canAdd('Add 1 to cart MKD 300'),true);
assert.deepEqual(R.cart('0 View order MKD 0'),{count:0,total:0});
assert.deepEqual(R.cart('2 View order MKD 1,200'),{count:2,total:1200});
assert.equal(R.cart('View order MKD 0'),null,'unknown cart quantity must fail closed');
assert.deepEqual(R.cart('View order (2 items) MKD 394'),{count:2,total:394});
assert.deepEqual(R.cart('View order 2 items MKD 394'),{count:2,total:394});
assert.equal(R.emptyCart('Your cart is empty'),true);
assert.equal(R.emptyCart('The cart is empty'),true);
assert.equal(R.emptyCart('Your cart is empty? Add more for free delivery'),false);
const restaurant={restaurant_id:1,restaurant_name:'Fixture',url:'https://wolt.com/en/mkd/skopje/restaurant/fixture',meals:[{menu_item_id:1,name:'Chicken',quantity:1,price_mkd:300}]};
assert.throws(()=>R.validateRestaurant({...restaurant,url:'https://evil.test/'}));
const itemUrl=restaurant.url+'/hummus-itemid-fc93cedb6aacc35a15e397b7';
assert.equal(R.venueKey(itemUrl),R.venueKey(restaurant.url));
assert.throws(()=>R.validateRestaurant({...restaurant,url:itemUrl}));
assert.throws(()=>R.venueKey(restaurant.url+'/checkout'));
assert.notEqual(R.venueKey(itemUrl),R.venueKey('https://wolt.com/en/mkd/skopje/restaurant/another'));

assert.throws(()=>R.validateRestaurant({...restaurant,meals:[{...restaurant.meals[0],quantity:2}]}));

async function backgroundTest(){
  let listener,removed,store={},created=0;
  const updates=[];
  const chrome={storage:{session:{get:async()=>store,set:async data=>{store=structuredClone(data);}}},tabs:{create:async()=>({id:++created}),update:async(id,props)=>{updates.push([id,props]);},onRemoved:{addListener(fn){removed=fn;}}},runtime:{onMessage:{addListener(fn){listener=fn;}}}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/background.js','utf8'),{importScripts(){},WoltCartRules:R,chrome,URL,Date,Promise,Object,Error,String,Number});
  const send=(message,sender)=>new Promise(resolve=>listener(message,sender,resolve));
  const sender={tab:{id:99,url:'http://localhost:8000/'},frameId:0};
  const request={type:'PREPARE',handoffKey:'generation:2026-09-20:1',restaurant};
  assert.equal((await send(request,{...sender,tab:{url:'https://evil.test/'}})).ok,false);
  assert.equal((await send(request,sender)).ok,true);
  assert.equal(created,1);
  assert.equal((await send(request,sender)).ok,true);
  assert.equal(created,1,'repeated handoff must reuse the same Wolt tab');
  const wolt={tab:{id:1,url:restaurant.url},frameId:0};
  assert.equal((await send({type:'CLAIM'},wolt)).canRun,true);
  assert.equal((await send({type:'CLAIM'},wolt)).canRun,false,'reload cannot replay additions');
  assert.equal((await send({...request,handoffKey:'another:day:1'},sender)).ok,false,'do not run concurrent cart preparations');
  await send({type:'UPDATE',status:'stopped',added:0,attempted:false},wolt);
  assert.equal((await send({type:'RETRY'},wolt)).ok,true,'retry allowed before any add attempt');
  assert.equal((await send({type:'CLAIM'},wolt)).canRun,true);
  await send({type:'UPDATE',status:'needs_attention',added:0,attempted:true},wolt);
  assert.equal((await send({type:'RETRY'},wolt)).ok,false,'uncertain addition must never replay');
  await send({type:'UPDATE',status:'review_ready',added:1},wolt);
  assert.equal((await send({...request,handoffKey:'another:day:1'},sender)).ok,true);
  assert.equal(created,2);
}

async function contentTest({existing=false,options=false,price=300,stopEarly=false,emptyState=false,accessibleCart=false,drawerCart=false,siblingCard=false,presetOptions=false,occupiedDrawer=false,mealCount=1,delayedOrders=false,itemNavigation=false,duplicateCards=false,conflictingDuplicate=false,promotionBadge=false}={}) {
  let count=existing?1:0,total=existing?100:0,opened=false,addClicks=0,checkoutClicks=0;
  const plannedRestaurant={...restaurant,meals:Array.from({length:mealCount},(_,i)=>({menu_item_id:i+1,name:['Chicken','Rice bowl','Salad'][i],quantity:1,price_mkd:300}))};
  let activeTitle;
  const location={href:restaurant.url};
  const reports=[];
  const noop=()=>{};
  const node=(text='')=>({innerText:text,textContent:text,disabled:false,getClientRects:()=>[{}],getAttribute:()=>null,addEventListener:noop,querySelectorAll:()=>[],append:noop});
  let drawerOpen=false;
  let controlReads=0;
  const status=node(),stop=node(),list=node(),retry=node();
  let onStop; stop.addEventListener=(type,fn)=>{onStop=fn;};
  const shadow={innerHTML:'',querySelector:selector=>({'#status':status,'#stop':stop,'#meals':list,'#retry':retry}[selector])};
  const host={style:{},attachShadow:()=>shadow};
  const cart=node();Object.defineProperty(cart,'innerText',{get:()=>accessibleCart?'View order':`${count} View order MKD ${total}`});cart.click=noop;cart.getAttribute=name=>name==='aria-label'&&accessibleCart?`${count} View order MKD ${total}`:null;
  const add=node(`Add to order · MKD ${price}`);add.click=()=>{addClicks++;count++;total+=price;opened=false;location.href=restaurant.url;};
  const checkout=node('Go to checkout');checkout.click=()=>checkoutClicks++;
  const payment=node('Pay MKD 300');payment.click=()=>{throw Error('Payment must never be clicked');};
  const titles=(duplicateCards ? [...plannedRestaurant.meals,...plannedRestaurant.meals] : plannedRestaurant.meals).map((meal,index)=>{
    const title=node(meal.name);
    const card=node(`${meal.name} MKD 300${promotionBadge && index>=mealCount ? " 20% off for W+" : ""}`);
    card.click=()=>{opened=true;activeTitle=title;if(itemNavigation)location.href=restaurant.url+'/hummus-itemid-fc93cedb6aacc35a15e397b7';};
    title.parentElement=card;title.closest=()=>siblingCard?null:card;
    card.querySelectorAll=selector=>selector==='p'?[node(conflictingDuplicate && index>=mealCount ? 'Different filling' : 'Same filling')]:selector==='[data-test-id="horizontal-item-card-price"]'?[node('MKD 300')]:selector.startsWith('button[data-test-id=')?[card]:[title];
    return title;
  });
  const orders=node();orders.getAttribute=key=>key==='aria-label'?'Your orders':null;orders.click=()=>{drawerOpen=true;};
  const drawerClose=node();drawerClose.getAttribute=key=>key==='aria-label'?'Close the dialog':null;drawerClose.click=()=>{drawerOpen=false;};
  const drawer=node(occupiedDrawer?'Item subtotal: MKD200':'Your cart is empty');
  drawer.getClientRects=()=>drawerOpen?[{}]:[];
  drawer.querySelectorAll=selector=>selector==='button,a,[role="button"]'?[drawerClose]:occupiedDrawer?[]:[node('Your cart is empty')];
  let manualCapture;
  const dialog=node('Chicken MKD 300');
  dialog.addEventListener=(type,fn)=>{manualCapture=fn;};
  dialog.removeEventListener=()=>{manualCapture=null;};
  dialog.querySelectorAll=selector=>{
    if(selector.startsWith('h1'))return [activeTitle];
    if(selector.startsWith('input[type="radio"]'))return presetOptions?[{...node(),tagName:'INPUT',type:'radio',name:'choice',checked:true},{...node(),tagName:'INPUT',type:'radio',name:'choice',checked:false}]:options?[node()]:[];
    if(selector.startsWith('input[type="number"]'))return [];
    if(selector==='button,a,[role="button"]')return [add];
    return [];
  };
  const document={documentElement:{append:noop},createElement:tag=>tag==='aside'?host:node(),querySelectorAll:selector=>{
    if(selector==='h1')return [node('Fixture')];
    if(selector==='[data-test-id="visible-baskets.modal"]')return drawerOpen?[drawer]:[];
    if(selector==='button,a,[role="button"]' && delayedOrders && ++controlReads<4)return [];
    if(selector==='button,a,[role="button"]')return drawerCart && count===0 ? [orders,checkout,payment] : emptyState && count===0 ? [checkout,payment] : [cart,checkout,payment];
    if(selector==='p,h2,h3,h4,span,div,[role="status"]')return emptyState && count===0 ? [node('The cart is empty')] : [];
    if(selector==='h2,h3,h4,[role="heading"]')return titles;
    if(selector==='[role="dialog"],dialog[open]')return drawerOpen?[drawer]:opened?[dialog]:[];
    return [];
  }};
  const chrome={runtime:{sendMessage:async data=>{
    if(data.type==='CLAIM')return {restaurant:plannedRestaurant,canRun:true,added:0,attempted:false};
    reports.push(data);
    if(options && data.message?.startsWith('Choose any extras')) {
      setTimeout(()=>{manualCapture({target:add});add.click();},10);
    }
    if(stopEarly && data.status==='preparing') onStop({currentTarget:stop});
    return {ok:true};
  }}};
  await vm.runInNewContext(fs.readFileSync(__dirname+'/wolt.js','utf8'),{document,chrome,WoltCartRules:R,location,getComputedStyle:()=>({visibility:'visible'}),Date,Number,Set,Error,Promise,setTimeout});
  if(stopEarly){assert.equal(addClicks,0);assert.equal(reports.at(-1).status,'stopped');assert.match(status.textContent,/helper added nothing/);assert.equal(retry.hidden,false);}
  else if(existing || occupiedDrawer || conflictingDuplicate || (price!==300 && !options)){assert.equal(addClicks,0);assert.equal(reports.at(-1).status,'needs_attention');
    const original=status.textContent;const reportCount=reports.length;onStop({currentTarget:stop});
    assert.equal(status.textContent,original,'click after failure must preserve the cause');assert.equal(reports.length,reportCount);}
  else {assert.equal(addClicks,mealCount);assert.equal(count,mealCount);assert.equal(total,price*mealCount);assert.equal(reports.at(-1).added,mealCount);assert.equal(checkoutClicks,1);assert.equal(reports.at(-1).status,'review_ready');}
}
(async()=>{await backgroundTest();await contentTest({options:true,mealCount:3,price:350});await contentTest({mealCount:3,duplicateCards:true,promotionBadge:true,siblingCard:true});await contentTest({mealCount:3,duplicateCards:true,siblingCard:true});await contentTest({duplicateCards:true,conflictingDuplicate:true});await contentTest();await contentTest({mealCount:3,drawerCart:true,siblingCard:true,presetOptions:true,delayedOrders:true,itemNavigation:true});await contentTest({mealCount:3});await contentTest({drawerCart:true,siblingCard:true,presetOptions:true});await contentTest({drawerCart:true,occupiedDrawer:true});await contentTest({emptyState:true});await contentTest({accessibleCart:true});await contentTest({stopEarly:true});await contentTest({existing:true});await contentTest({options:true});await contentTest({price:350});console.log('Cart helper checks passed: origin scope, deduplication, reload protection, empty-cart requirement, price/options checks, and no payment clicks.');})().catch(error=>{console.error(error);process.exitCode=1;});
