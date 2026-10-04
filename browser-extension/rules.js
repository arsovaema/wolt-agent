/* Shared pure validation. No credentials, network calls, or payment actions. */
(function(root) {
  const normalize = value => String(value || '').normalize('NFKC').replace(/\s+/g,' ').trim();
  const appOrigin = value => {
    try { return ['http://127.0.0.1:8000','http://localhost:8000'].includes(new URL(value).origin); }
    catch { return false; }
  };
  function venueKey(value, allowItem=true) {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.hostname !== 'wolt.com' || url.port || url.username || url.password) throw Error('Unsupported restaurant URL');
    const parts = url.pathname.replace(/\/$/,'').split('/').filter(Boolean);
    const itemPage=allowItem && parts.length===6 && /^.+-itemid-[a-f0-9]{24}$/i.test(parts[5]);
    if ((parts.length !== 5 && !itemPage) || !['restaurant','venue'].includes(parts[3])) throw Error('Expected a Wolt restaurant page');
    return [parts[1],parts[2],parts[4]].join('/');
  }
  function validateRestaurant(data) {
    if (!data || !Number.isSafeInteger(data.restaurant_id) || data.restaurant_id < 1) throw Error('Invalid restaurant');
    venueKey(data.url,false);
    if (typeof data.restaurant_name !== 'string' || !data.restaurant_name.trim() || data.restaurant_name.length>150) throw Error('Invalid restaurant name');
    if (!Array.isArray(data.meals) || !data.meals.length || data.meals.length>3) throw Error('Expected one to three meals');
    const ids = new Set();
    const meals = data.meals.map(meal => {
      if (!Number.isSafeInteger(meal.menu_item_id) || ids.has(meal.menu_item_id) || meal.menu_item_id<1 || meal.quantity!==1
          || !Number.isSafeInteger(meal.price_mkd) || meal.price_mkd<=0 || meal.price_mkd>100000
          || typeof meal.name!=='string' || !meal.name.trim() || meal.name.length>150) throw Error('Invalid meal selection');
      ids.add(meal.menu_item_id);
      return {menu_item_id:meal.menu_item_id,name:meal.name,quantity:1,price_mkd:meal.price_mkd};
    });
    const url = new URL(data.url); url.search=''; url.hash='';
    return {restaurant_id:data.restaurant_id,restaurant_name:data.restaurant_name,url:url.href,meals};
  }
  function number(value) {
    const raw=value.replace(/\s/g,'');
    if (/^\d+$/.test(raw)) return Number(raw);
    if (/^\d+[.,]\d{1,2}$/.test(raw)) return Number(raw.replace(',','.'));
    if (/^\d{1,3}(,\d{3})+(\.\d{2})?$/.test(raw)) return Number(raw.replace(/,/g,''));
    if (/^\d{1,3}(\.\d{3})+(,\d{2})?$/.test(raw)) return Number(raw.replace(/\./g,'').replace(',','.'));
    return NaN;
  }
  function prices(value) {
    const text=normalize(value);
    const hits=[...text.matchAll(/MKD\s*([0-9][0-9., ]*)|([0-9][0-9., ]*)\s*MKD/gi)].map(hit=>number((hit[1]||hit[2]).trim()));
    return hits.every(Number.isFinite) ? [...new Set(hits)] : [];
  }
  function cart(value) {
    const text=normalize(value);
    // Accept count before or after the label, without confusing the price
    // with a quantity. An absent count is still unknown, not an empty cart.
    const match=text.match(/^(\d+)\s+(?:items?\s+)?View (?:order|cart)\b/i)
      || text.match(/^View (?:order|cart)\s*\((\d+)(?: items?)?\)/i)
      || text.match(/^View (?:order|cart)\s+(\d+)\s+items?\b/i);
    const amounts=prices(text);
    return match && amounts.length===1 ? {count:Number(match[1]),total:amounts[0]} : null;
  }
  function emptyCart(value) {
    return /^(?:(?:Your|The) (?:cart|basket|order) is empty|(?:Cart|Basket) is empty)[.!]?$/i.test(normalize(value));
  }
  function canAdd(value) {
    return /^Add(?: 1)? to (?:order|cart|basket)(?: [·•–—-]?\s*(?:MKD [\d., ]+|[\d., ]+ MKD))?$/i.test(normalize(value));
  }
  const api={normalize,appOrigin,venueKey,validateRestaurant,prices,cart,emptyCart,canAdd};
  root.WoltCartRules=api;
  if (typeof module!=='undefined') module.exports=api;
})(globalThis);
