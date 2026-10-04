const $ = (selector) => document.querySelector(selector);
const form = $('#profile-form');
const generateButton = $('#generate-button');
const profileStatus = $('#profile-status');
const planStatus = $('#plan-status');
let profileSaved = false;
let generating = false;
let pollTimer;
let sessionEpoch = 0;
let pendingGeneration = false;
let visibleDays = [];

function showStatus(node, message, error = false) {
  node.textContent = message;
  node.classList.toggle('error', error);
}

function formPayload() {
  const data = Object.fromEntries(new FormData(form).entries());
  for (const key of ['current_weight_kg', 'target_weight_kg', 'height_cm']) data[key] = Number(data[key]);
  for (const key of ['age', 'daily_budget_mkd', 'daily_fee_allowance_mkd']) data[key] = Number(data[key]);
  const [hour, minute] = (data.delivery_time || '13:00').split(':').map(Number);
  data.delivery_minute = hour * 60 + minute;
  delete data.delivery_time;
  data.city = 'Skopje';
  data.latitude = null;
  data.longitude = null;
  return data;
}

async function request(url, options = {}) {
  const epoch = sessionEpoch;
  const response = await fetch(url, { headers: {'Content-Type': 'application/json'}, ...options });
  const body = await response.json().catch(() => ({}));
  if (epoch !== sessionEpoch) throw new Error('Session changed; please reload your plan.');
  if (response.status === 401) {
    clearTimeout(pollTimer);
    profileSaved = false;
    pendingGeneration = false;
    form.reset();
    closeOrderDialog();
    showStatus(profileStatus, '');
    $('#auth-panel').classList.remove('hidden');
    $('.profile-panel').classList.add('hidden');
    $('#plan-section').classList.add('hidden');
  }
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : body.message || `Request failed (${response.status})`);
  return body;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const budget = Number(form.elements.namedItem('daily_budget_mkd').value);
  const fees = Number(form.elements.namedItem('daily_fee_allowance_mkd').value);
  if (fees >= budget) {
    showStatus(profileStatus, 'The daily fee allowance must be less than the daily budget, so there is money left for meals.', true);
    return;
  }
  const button = $('#save-button');
  button.disabled = true;
  showStatus(profileStatus, 'Calculating your targets…');
  try {
    const result = await request('/api/user/profile', {method: 'POST', body: JSON.stringify(formPayload())});
    profileSaved = true;
    generateButton.disabled = false;
    showStatus(profileStatus, 'Profile saved. Your previous week was cleared.');
    renderProfileSummary(result.profile);
    pendingGeneration = false;
    await buildWeek();
  } catch (error) {
    showStatus(profileStatus, error.message, true);
  } finally { button.disabled = false; }
});

generateButton.addEventListener('click', buildWeek);

async function buildWeek() {
  if (generating || pendingGeneration || !profileSaved) return;
  clearTimeout(pollTimer);
  generating = true;
  generateButton.disabled = true;
  $('#save-button').disabled = true;
  generateButton.textContent = 'Building your week…';
  renderMessage('Finding restaurant meals for your goal and building your seven-day plan…');
  try {
    const plan = await request('/api/agent/weekly-plan', {method: 'POST'});
    renderPlan(plan);
    if (plan.status === 'refreshing') await loadWeeklyPlan();
    if (plan.weekly_plan?.length) $('#plan-section').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) { renderMessage(error.message, true); }
  finally {
    generating = false;
    generateButton.disabled = pendingGeneration || !profileSaved;
    $('#save-button').disabled = false;
    generateButton.textContent = 'Generate 7-day plan';
  }
}

async function loadWeeklyPlan() {
  const plan = await request('/api/agent/weekly-plan');
  renderPlan(plan);
  if (plan.status === 'refreshing') {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(() => loadWeeklyPlan().catch(error => renderMessage(error.message, true)), 2500);
  }
  return plan;
}

async function initialize() {
  try {
    const config = await request('/api/auth/config');
    $('#logout-button').classList.toggle('hidden', !config.required);
    const saved = await request('/api/user/profile');
    $('#auth-panel').classList.add('hidden');
    $('.profile-panel').classList.remove('hidden');
    $('#plan-section').classList.remove('hidden');
    profileSaved = false;
    if (saved.profile) {
      profileSaved = true;
      for (const [name, value] of Object.entries(saved.profile)) {
        const input = form.elements.namedItem(name);
        if (input && value != null) input.value = value;
      }
      if (saved.profile.delivery_minute != null) form.elements.namedItem('delivery_time').value = `${String(Math.floor(saved.profile.delivery_minute / 60)).padStart(2,'0')}:${String(saved.profile.delivery_minute % 60).padStart(2,'0')}`;
      renderProfileSummary(saved.profile);
      generateButton.disabled = false;
    }
    const plan = await loadWeeklyPlan();
    if (profileSaved && plan.status === 'refresh_required') await buildWeek();
  } catch (error) { renderMessage(error.message, true); }
}

function renderProfileSummary(profile) {
  const goal = profile.goal || 'your goal';
  showStatus(profileStatus, `Saved for ${goal}. Targets: ${profile.daily_calorie_target} kcal · ${Math.round(profile.daily_protein_target)} g protein.`);
}

function renderMessage(message, error = false) {
  visibleDays = [];
  closeOrderDialog();
  planStatus.classList.remove('hidden');
  planStatus.innerHTML = `<div class="empty-icon">${error ? '!' : '✦'}</div><p>${escapeHtml(message)}</p>`;
  $('#week-summary').classList.add('hidden');
  $('#week-grid').innerHTML = '';
}

function renderPlan(plan) {
  pendingGeneration = plan?.status === 'refreshing';
  const days = plan?.weekly_plan || [];
  visibleDays = days;
  closeOrderDialog();
  if (!days.length) {
    renderMessage(plan?.message || 'Set your goal to build your week.', plan?.status === 'refresh_failed');
    generateButton.disabled = generating || pendingGeneration || !profileSaved;
    return;
  }
  generateButton.disabled = generating || pendingGeneration || !profileSaved;
  planStatus.classList.toggle('hidden', plan.status === 'ready');
  if (plan.status !== 'ready') showStatus(planStatus, plan.message || 'Closest available plan');
  const summary = $('#week-summary');
  summary.classList.remove('hidden');
  const meals = days.flatMap(day => day.meals || []);
  const kcal = Math.round(days.reduce((sum, day) => sum + (day.nutrition?.estimated_totals?.calories || 0), 0) / Math.max(days.length, 1));
  const total = days.reduce((sum, day) => sum + (day.budget?.estimated_total_mkd || 0), 0);
  summary.innerHTML = `<div class="metric"><b>${days.length}</b><span>days planned</span></div><div class="metric"><b>${new Set(meals.map(m => `${m.restaurant_name}:${m.item_name}`)).size}</b><span>unique meals</span></div><div class="metric"><b>${kcal}</b><span>avg kcal / day</span></div><div class="metric"><b>${Math.round(total / Math.max(days.length, 1))} MKD</b><span>avg / day</span></div>`;
  $('#week-grid').innerHTML = days.map(renderDay).join('');
}

function renderDay(day) {
  const nutrition = day.nutrition?.estimated_totals || {};
  const meals = (day.meals || []).map(renderMeal).join('');
  return `<article class="day-card" role="button" tabindex="0" data-day-number="${Number(day.day_number)}" aria-label="Review ordering for day ${Number(day.day_number)}"><div class="day-top"><h3>Day ${day.day_number}</h3><span>${formatDate(day.date)}</span></div><p class="day-message">${escapeHtml(day.message || '')}</p>${meals}<div class="day-bottom"><span>${nutrition.calories || 0} kcal · ${nutrition.protein || 0} g protein</span><span>${day.budget?.estimated_total_mkd || 0} MKD est.</span></div><button type="button" class="day-order-hint" data-day-number="${Number(day.day_number)}">Review and order this day ↗</button></article>`;
}

function renderMeal(meal) {
  const ranges = meal.nutrition_ranges || {};
  const calories = ranges.calories?.low ? `${Math.round(ranges.calories.low)}–${Math.round(ranges.calories.high)} kcal` : `${Math.round(meal.calories)} kcal`;
  const image = meal.image_url ? `<img src="${escapeAttribute(meal.image_url)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'meal-image-placeholder',textContent:'✦'}))">` : '<div class="meal-image-placeholder">✦</div>';
  return `<div class="meal">${image}<div><h4>${escapeHtml(meal.item_name)}</h4><p class="restaurant">${escapeHtml(meal.restaurant_name)}</p><div class="meal-meta"><span class="pill">${calories}</span><span class="pill">${Math.round(meal.protein)} g protein</span><span class="pill price">${meal.price_mkd} MKD</span></div><div class="confidence">Nutrition confidence: <strong>${escapeHtml(meal.nutrition_confidence || 'low')}</strong></div></div></div>`;
}

function formatDate(value) { return new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric'}).format(new Date(`${value}T12:00:00`)); }
function escapeHtml(value) { return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
function escapeAttribute(value) { return escapeHtml(value).replace(/`/g, '&#96;'); }

async function authenticate(register = false) {
  const data = Object.fromEntries(new FormData($('#auth-form')).entries());
  try {
    await request('/api/auth/' + (register ? 'register' : 'login'), {method: 'POST', body: JSON.stringify(data)});
    sessionEpoch++;
    clearTimeout(pollTimer);
    profileSaved = false;
    pendingGeneration = false;
    form.reset();
    closeOrderDialog();
    showStatus(profileStatus, '');
    $('#auth-form').reset();
    await initialize();
  } catch (error) { showStatus($('#auth-status'), error.message, true); }
}
$('#auth-form').addEventListener('submit', event => { event.preventDefault(); authenticate(); });
$('#register-button').addEventListener('click', () => authenticate(true));
$('#logout-button').addEventListener('click', async () => {
  sessionEpoch++;
  await request('/api/auth/logout', {method: 'POST'});
  clearTimeout(pollTimer);
  profileSaved = false;
  pendingGeneration = false;
  form.reset();
  $('#week-grid').innerHTML = '';
  visibleDays = [];
  closeOrderDialog();
  await initialize();
});
let orderPreview = null;
let orderVersion = 0;
let helperAvailable = false;
let helperPreparing = false;
let activeOrderRequest = null;
let helperTimer;
let preparedRestaurants = new Set();

function closeOrderDialog() {
  orderVersion++;
  helperPreparing = false;
  orderPreview = null;
  clearTimeout(helperTimer);
  activeOrderRequest = null;
  if ($('#order-dialog').open) $('#order-dialog').close();
}

async function openOrderDay(dayNumber) {
  const day = visibleDays.find(day => Number(day.day_number) === Number(dayNumber));
  if (!day?.meals?.length) return;
  const version = ++orderVersion;
  orderPreview = null;
  preparedRestaurants = new Set();
  $('#order-title').textContent = `Do you want to order Day ${dayNumber}?`;
  $('#order-meals').innerHTML = '';
  $('#order-confirm').disabled = true;
  $('#order-confirm').textContent = 'Yes, prepare in Wolt';
  $('#order-setup').classList.toggle('hidden', helperAvailable);
  showStatus($('#order-status'), 'Checking the saved meals and restaurant links…');
  $('#order-dialog').showModal();
  window.postMessage({channel:'wolt-planner', type:'ping'}, window.location.origin);
  try {
    const result = await request(`/api/agent/weekly-plan/${dayNumber}/order-preview?planned_date=${encodeURIComponent(day.date)}`);
    if (version !== orderVersion) return;
    orderPreview = result;
    renderOrderPreview();
  } catch (error) {
    if (version === orderVersion) showStatus($('#order-status'), error.message, true);
  }
}

function renderOrderPreview() {
  if (!orderPreview) return;
  $('#order-meals').innerHTML = orderPreview.restaurants.map((restaurant,index) => `<section class="order-restaurant"><h3>${escapeHtml(restaurant.restaurant_name)}</h3><ul>${restaurant.meals.map(meal => `<li>${meal.quantity} × ${escapeHtml(meal.name)} · ${meal.price_mkd} MKD</li>`).join('')}</ul><a href="${escapeAttribute(restaurant.url)}" target="_blank" rel="noopener noreferrer">Open restaurant manually ↗</a>${index > 0 ? `<br><button class="button secondary" type="button" data-prepare-restaurant="${index}">Prepare this restaurant</button>` : ''}</section>`).join('');
  showStatus($('#order-status'), orderPreview.separate_checkouts ? `${orderPreview.restaurants.length} restaurants, ${orderPreview.food_subtotal_mkd} MKD in food. Each needs a separate checkout. Finish one in Wolt before preparing the next.` : `${orderPreview.food_subtotal_mkd} MKD in food. Wolt will show the current fees and checkout total.`);
  $('#order-confirm').disabled = !helperAvailable || helperPreparing;
  $('#order-setup').classList.toggle('hidden', helperAvailable);
}

async function prepareRestaurant(index = 0) {
  if (!orderPreview || !helperAvailable || helperPreparing) return;
  const restaurant = orderPreview.restaurants[index];
  if (!restaurant || preparedRestaurants.has(restaurant.restaurant_id)) {
    showStatus($('#order-status'), 'This restaurant was already opened. Continue in its Wolt tab; meals will not be added twice.');
    return;
  }
  const version = orderVersion;
  helperPreparing = true;
  $('#order-confirm').disabled = true;
  showStatus($('#order-status'), 'Checking your plan, then opening Wolt…');
  try {
    const latest = await request(`/api/agent/weekly-plan/${orderPreview.day_number}/order-preview?planned_date=${encodeURIComponent(orderPreview.date)}&generation=${encodeURIComponent(orderPreview.generation)}`);
    if (version !== orderVersion) { helperPreparing = false; return; }
    const current = latest.restaurants.find(group => group.restaurant_id === restaurant.restaurant_id);
    if (!current) throw new Error('Your restaurant selection changed. Reopen this day.');
    activeOrderRequest = crypto.randomUUID();
    window.postMessage({channel:'wolt-planner',type:'prepare',requestId:activeOrderRequest,handoffKey:`${latest.generation}:${latest.date}:${current.restaurant_id}`,restaurant:current},window.location.origin);
    helperTimer = setTimeout(() => {
      if (version !== orderVersion) return;
      showStatus($('#order-status'), 'The helper has not replied. Check the Wolt tab before retrying, so items are not duplicated.',true);
      helperPreparing = false;
    },15000);
  } catch (error) {
    helperPreparing = false;
    if (version === orderVersion) { showStatus($('#order-status'),error.message,true); $('#order-confirm').disabled=false; }
  }
}

window.addEventListener('message',event => {
  if (event.source !== window || event.origin !== window.location.origin || event.data?.channel !== 'wolt-cart-helper') return;
  if (event.data.type === 'ready') {
    helperAvailable = true;
    $('#order-setup').classList.add('hidden');
    $('#order-confirm').disabled = !orderPreview || helperPreparing;
    return;
  }
  if (event.data.requestId !== activeOrderRequest) return;
  clearTimeout(helperTimer);
  helperPreparing = false;
  if (event.data.ok) {
    preparedRestaurants.add(event.data.restaurantId);
    showStatus($('#order-status'),'Wolt is open. Follow the helper’s progress there, then review your delivery details and pay yourself.');
    $('#order-confirm').textContent='Opened in Wolt';
  } else {
    showStatus($('#order-status'),event.data.error || 'Could not open Wolt. Use the restaurant link.',true);
    $('#order-confirm').disabled=false;
  }
});

$('#week-grid').addEventListener('click',event => {
  const card=event.target.closest('[data-day-number]');
  if (card) openOrderDay(Number(card.dataset.dayNumber));
});
$('#week-grid').addEventListener('keydown',event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const card=event.target.closest('[data-day-number]');
  if (card) { event.preventDefault(); openOrderDay(Number(card.dataset.dayNumber)); }
});
$('#order-meals').addEventListener('click',event => {
  const button=event.target.closest('[data-prepare-restaurant]');
  if (button) prepareRestaurant(Number(button.dataset.prepareRestaurant));
});
$('#order-confirm').addEventListener('click',() => prepareRestaurant(0));
$('#order-cancel').addEventListener('click',closeOrderDialog);
$('#order-close').addEventListener('click',closeOrderDialog);
$('#order-dialog').addEventListener('cancel',closeOrderDialog);
$('#order-dialog').addEventListener('click',event => { if (event.target === $('#order-dialog')) closeOrderDialog(); });
initialize();
