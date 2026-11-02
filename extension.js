/* Twitch-hosted panel extension. Set API_BASE to the public HTTPS QuestCast backend before upload. */
const API_BASE = 'https://YOUR-QUESTCAST-HTTPS-HOST';
let auth = null;
const connection = document.getElementById('connection');
const questEl = document.getElementById('quest');
const storiesEl = document.getElementById('stories');
const walletEl = document.getElementById('wallet');
const shopEl = document.getElementById('shop');
async function request(path, method='GET', body) {
  const response = await fetch(`${API_BASE}/api/workspace/expansion/extension${path}`, {
    method, headers: {'Authorization': `Bearer ${auth.token}`, 'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await response.json().catch(()=>({}));
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}
async function refresh() {
  if (!auth) return;
  try {
    const data = await request('/state'); connection.textContent = 'CONNECTED';
    if (data.quest) {
      const q=data.quest;
      questEl.innerHTML=`<small>ACTIVE COMMUNITY QUEST</small><h1></h1><p></p><div class="progress"><i></i></div><p class="progress-copy"></p>`;
      questEl.querySelector('h1').textContent=q.title;
      questEl.querySelector('p').textContent=q.description||q.objective;
      questEl.querySelector('.progress i').style.width=`${q.percent}%`;
      questEl.querySelector('.progress-copy').textContent=`${q.progress} / ${q.target} · ${q.percent}% complete`;
    } else { questEl.innerHTML='<small>ACTIVE COMMUNITY QUEST</small><h1>No active quest</h1><p>The creator has not started the next chapter yet.</p>'; }
    storiesEl.replaceChildren();
    for (const story of data.stories) {
      const title=document.createElement('p'); title.textContent=story.title; storiesEl.append(title);
      for (const choice of story.choices) {
        const button=document.createElement('button'); button.className='choice';
        button.textContent=choice.label;
        const count=document.createElement('small'); count.textContent=String(story.tally[choice.key]||0); button.append(count);
        button.addEventListener('click',async()=>{button.disabled=true;try{await request(`/story/${story.id}/vote`,'POST',{choice_key:choice.key});await refresh()}catch(e){connection.textContent='VOTE FAILED';button.disabled=false;}});
        storiesEl.append(button);
      }
    }
    if (!data.stories.length) storiesEl.textContent='No open community decision right now.';
    const wallet=await request('/wallet');
    walletEl.textContent=`Balance: ${wallet.balance} Quest Coins`;
    shopEl.replaceChildren();
    for (const [key,item] of Object.entries(wallet.catalog||{})) {
      const buy=document.createElement('button');buy.className='choice';buy.textContent=`Buy ${item.name} · ${item.cost} coins`;
      buy.addEventListener('click',async()=>{buy.disabled=true;try{await request('/economy/purchase','POST',{item_key:key});await refresh()}catch(e){connection.textContent='PURCHASE FAILED';buy.disabled=false;}});
      shopEl.append(buy);
      const owned=(wallet.inventory||[]).find(x=>x.item_key===key);
      if(owned?.quantity>0){const use=document.createElement('button');use.className='choice';use.textContent=`Use ${item.name} · owned ${owned.quantity}`;use.addEventListener('click',async()=>{use.disabled=true;try{await request('/economy/use','POST',{item_key:key});await refresh()}catch(e){connection.textContent='ABILITY FAILED';use.disabled=false;}});shopEl.append(use);}
    }
  } catch(e) { connection.textContent='NOT CONNECTED'; questEl.innerHTML=`<p class="error"></p>`; questEl.querySelector('p').textContent=e.message; }
}
if (window.Twitch?.ext) {
  window.Twitch.ext.onAuthorized(newAuth=>{auth=newAuth;refresh();});
  window.Twitch.ext.onError(()=>{connection.textContent='TWITCH ERROR';});
} else { connection.textContent='PREVIEW ONLY'; questEl.innerHTML='<p class="error">Open this panel inside Twitch Extension testing to authorize it.</p>'; }
