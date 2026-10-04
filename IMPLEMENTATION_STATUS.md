// Execute the real frontend script against a minimal DOM and mocked HTTP API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('frontend/app.js', 'utf8');
const html = fs.readFileSync('frontend/index.html', 'utf8');
assert(!html.includes('search-form'));
assert(!html.includes('Describe your meal'));
function element() {
  return {textContent: '', innerHTML: '', disabled: true, value: '', listeners: {},
    classList: {add(){}, remove(){}, toggle(){}}, addEventListener(type, fn) {this.listeners[type] = fn;},
    scrollIntoView(){}, reset(){}, open:false, showModal(){this.open=true;}, close(){this.open=false;}};
}
const meal = {item_name:'Test meal', restaurant_name:'Test restaurant', calories:600, protein:30, price_mkd:200};
const days = Array.from({length:7}, (_,i) => ({day_number:i+1,date:`2026-09-${20+i}`,status:'poor_fit',message:'Affordable alternative',meals:[meal,meal,meal],nutrition:{estimated_totals:{calories:1800}},budget:{estimated_total_mkd:800}}));
async function scenario(initialStatus) {
  const nodes = new Map();
  const get = id => {if(!nodes.has(id)) nodes.set(id,element());return nodes.get(id);};
  const fields = new Map();
  get('#profile-form').elements = {namedItem(name){if(!fields.has(name)) fields.set(name,element());return fields.get(name);}};
  get('#profile-form').reset = () => {for (const input of fields.values()) input.value = '';};
  const calls = [];
  const browserWindow={location:{origin:'http://127.0.0.1:8000'},listeners:{},messages:[],addEventListener(type,fn){this.listeners[type]=fn;},postMessage(message){this.messages.push(message);}};
  const context = {window:browserWindow,crypto:{randomUUID:()=> 'test-request'},encodeURIComponent,document:{querySelector:get},Intl,Date,Number,Set,Map,String,Object,console,
    setTimeout:()=>1,clearTimeout(){},FormData:class {entries(){return [...fields].map(([k,v])=>[k,v.value]);}},
    fetch:async (url,options={}) => {
      calls.push([url,options.method || 'GET']);
      const body = url === '/api/user/profile' ? {profile:{goal:'muscle gain',daily_calorie_target:2500,daily_protein_target:100,daily_budget_mkd:1000,daily_fee_allowance_mkd:200}} :
        {status:options.method === 'POST' ? 'poor_fit' : initialStatus, message:'Week ready',weekly_plan:initialStatus==='refresh_required' && options.method!=='POST' ? [] : days};
      return {ok:true,json:async()=>body};
    }};
  vm.runInNewContext(source,context);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal((get('#week-grid').innerHTML.match(/class="day-card"/g)||[]).length,7);
  assert.equal(get('#generate-button').disabled,false);
  assert.equal(fields.get('daily_fee_allowance_mkd').value,200);
  if(initialStatus==='refresh_required') assert(calls.some(([url,method])=>url==='/api/agent/weekly-plan'&&method==='POST'));
  const before=calls.length;
  fields.get('daily_fee_allowance_mkd').value=1200;
  await get('#profile-form').listeners.submit({preventDefault(){}});
  assert.equal(calls.length,before,'invalid fee must not call profile API');
  fields.get('daily_fee_allowance_mkd').value=200;
  await get('#profile-form').listeners.submit({preventDefault(){}});
  assert(calls.slice(before).some(([url,method])=>url==='/api/agent/weekly-plan'&&method==='POST'),'saving goal must build week');
  const group={restaurant_id:1,restaurant_name:'Test restaurant',url:'https://wolt.com/en/mkd/skopje/restaurant/fixture',food_subtotal_mkd:600,meals:[1,2,3].map(id=>({menu_item_id:id,name:`Test meal ${id}`,quantity:1,price_mkd:200}))};
  const preview={day_number:1,date:days[0].date,generation:'test-generation',restaurants:[group],food_subtotal_mkd:600,separate_checkouts:false};
  context.fetch=async()=>({ok:true,json:async()=>preview});
  days[0].day_number = '1';
  get('#week-grid').listeners.click({target:{closest:()=>({dataset:{dayNumber:'1'}})}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(get('#order-dialog').open,true);
  assert.equal(get('#order-confirm').disabled,true,'helper must be connected before automatic preparation');
  browserWindow.listeners.message({source:browserWindow,origin:browserWindow.location.origin,data:{channel:'wolt-cart-helper',type:'ready'}});
  assert.equal(get('#order-confirm').disabled,false);
  await context.prepareRestaurant(0);
  const handoff=browserWindow.messages.find(message=>message.type==='prepare');
  assert.equal(handoff.restaurant.restaurant_id,1);
  assert.equal(handoff.restaurant.meals.length,3,'one handoff must include the full restaurant day');
  assert(handoff.handoffKey.includes(preview.generation));
  browserWindow.listeners.message({source:browserWindow,origin:browserWindow.location.origin,data:{channel:'wolt-cart-helper',requestId:handoff.requestId,ok:true,restaurantId:1}});
  const sent=browserWindow.messages.length;
  await context.prepareRestaurant(0);
  assert.equal(browserWindow.messages.length,sent,'repeated confirmation must not add meals twice');
  context.closeOrderDialog();
  assert.equal(get('#order-dialog').open,false);
  context.fetch = async () => ({ok:false,status:401,json:async()=>({detail:'Sign in'})});
  await assert.rejects(context.request('/api/private'));
  assert.equal(fields.get('daily_budget_mkd').value,'','expired session must clear previous user fields');

}
(async()=>{await scenario('poor_fit');await scenario('refresh_required');console.log('Frontend checks passed: seven days, profile restoration, auto-generation, fee validation, no search form.');})().catch(error=>{console.error(error);process.exitCode=1;});

Live test found delayed Wolt header hydration: the title rendered before Your orders, causing the old fallback to wait for a nonexistent inline cart. Version 0.1.4 waits for the header/drawer control explicitly; the three-meal fixture now delays the header and passes. Awaiting reload for another live run; no items added in the failed v0.1.3 run.

Live v0.1.4 run verified the drawer and meal-card steps, then exposed Wolt's item route (`/<dish>-itemid-<24 hex chars>`). The venue guard previously rejected that same-restaurant route. v0.1.5 accepts only that observed item-route format for in-progress navigation, while handoff URLs remain restricted to restaurant roots. The three-meal fixture now changes URLs on item open and returns on Add; tests pass. No cart addition occurred during the v0.1.4 failure. Waiting for v0.1.5 installation to complete live validation.

## Live three-meal test passed — v0.1.5

On 2026-09-21, after the user loaded v0.1.5, the planner's Day 5 confirmation launched one helper handoff. The extension automatically added exactly one Hummus (89 MKD), one Grilled Halloumi Naanwich (Vegetarian) (242 MKD), and one Heritage Naanwich (242 MKD), preserving Wolt's preset No thanks extra-protein option. The visible cart showed 3 items and 573 MKD; its drawer showed each quantity as 1 and helper status Meals added. This was the extension's complete run, not manual per-item additions. The verified cart is left open. No order was submitted or paid for.

Result: 72/72 Python tests, frontend checks, helper regression scenarios, and this live three-item cart scenario passed. The generated live week has 7 distinct venues, 1 venue and 3 meals per day. This supersedes the pending-live-test notes above. It does not claim an 80% success rate across all Wolt restaurants; only this live restaurant scenario has been validated end to end.
