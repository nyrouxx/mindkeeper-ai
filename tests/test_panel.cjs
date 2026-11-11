const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const app = {innerHTML:'',querySelector:()=>({})};
const dialog = {innerHTML:'',showModal(){},close(){}};
class Element {
  attachShadow(){this.shadowRoot={innerHTML:'',addEventListener(){},querySelector:selector=>selector==='.app'?app:dialog};}
}
let Panel;
const context = {HTMLElement:Element,customElements:{get:()=>null,define:(_,value)=>Panel=value},Intl,Date,Set,console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../custom_components/personal_secretary/frontend/panel.js'),'utf8')+'\nglobalThis.utils={zoned,monthDays,onDay,esc};',context);
const utils=context.utils;
const data=()=>({categories:['Работа','Здоровье'],priorities:['Обычная','Важная'],timezone:'Europe/Moscow',conversation:[],tasks:[{id:'1',title:'Врач <img src=x onerror=alert(1)>',status:'active',due_at:'2026-11-10T22:00:00Z',category:'Здоровье',priority:'Важная'},{id:'2',title:'Встреча',status:'active',due_at:'2026-11-11T12:00:00Z',category:'Работа',priority:'Обычная'}]});

test('calendar grid has full Monday-first weeks including month boundaries',()=>{
  for(let month=0;month<12;month++){
    const days=utils.monthDays(new Date(2026,month,1));
    assert.equal(days.length,42);assert.equal(days[0].getDay(),1);assert.equal(days.at(-1).getDay(),0);
    assert(days.some(d=>d.getMonth()===month && d.getDate()===1));
  }
});
test('event is shown on its HA local date, not UTC day',()=>{
  const parts=utils.zoned('2026-11-10T22:00:00Z','Europe/Moscow');
  assert.equal(parts.day,'2026-11-11');assert.equal(parts.time,'01:00');
});
test('renders calendar and category/importance filters; task text cannot inject HTML',()=>{
  const panel=new Panel();panel.data=data();panel.month=new Date(2026,10,1);panel._hass={};panel.render();
  assert(app.innerHTML.includes('Календарь'));assert(app.innerHTML.includes('Здоровье'));assert(app.innerHTML.includes('Важная'));
  assert(app.innerHTML.includes('&lt;img'));assert(!app.innerHTML.includes('<img src=x'));
  panel.category='Работа';assert.equal(panel.tasks().length,1);assert.equal(panel.tasks()[0].id,'2');
  panel.category='';panel.priority='Важная';assert.equal(panel.tasks().length,1);assert.equal(panel.tasks()[0].id,'1');
});
test('task editor exposes editable tags and HA local time',()=>{
  const panel=new Panel();panel.data=data();panel.edit(panel.data.tasks[0]);
  assert(dialog.innerHTML.includes('Категория'));assert(dialog.innerHTML.includes('Важность'));assert(dialog.innerHTML.includes('value="2026-11-11"'));assert(dialog.innerHTML.includes('value="01:00"'));
  assert(dialog.innerHTML.includes('&lt;img'));assert(!dialog.innerHTML.includes('<img'));
});
test('settings let user add categories and reorder importance',()=>{
  const panel=new Panel();panel.data=data();panel.settings();
  assert(dialog.innerHTML.includes('По одному названию в строке'));
  assert(dialog.innerHTML.includes('Первый уровень'));
});

test('multi-day event spans the calendar but excludes its midnight end',()=>{
  const task={due_at:'2026-11-10T12:00:00+03:00',end_at:'2026-11-13T00:00:00+03:00'};
  assert(utils.onDay(task,'2026-11-10','Europe/Moscow'));
  assert(utils.onDay(task,'2026-11-12','Europe/Moscow'));
  assert(!utils.onDay(task,'2026-11-13','Europe/Moscow'));
});

test('Telegram settings show selected bot and only its allowed recipients',()=>{
  const panel=new Panel();panel.data=data();panel.data.telegram={route:{bot_entry_id:'a',chat_id:42,user_id:42},bot_label:'Alpha',chat_label:'Personal',bots:[{value:'a',label:'Alpha'},{value:'b',label:'Beta'}],chats:[{bot_entry_id:'a',chat_id:42,label:'Alpha private'},{bot_entry_id:'b',chat_id:84,label:'Beta private'},{bot_entry_id:'b',chat_id:-10084,label:'Beta group'}]};
  panel.settings();assert(dialog.innerHTML.includes('Telegram'));assert(dialog.innerHTML.includes('Alpha'));
  panel.telegramSettings('a');assert(dialog.innerHTML.includes('Alpha private'));assert(!dialog.innerHTML.includes('Beta private'));
  panel.telegramSettings('b');assert(dialog.innerHTML.includes('Beta private'));assert(dialog.innerHTML.includes('Beta group'));assert(!dialog.innerHTML.includes('Alpha private'));
  assert(dialog.innerHTML.includes('Telegram ID'));assert(dialog.innerHTML.includes('telegram_bot'));
});

test('repeat mode is a separate editable task label and defaults off',()=>{
  const panel=new Panel();panel.data=data();panel.edit(panel.data.tasks[0]);
  assert(dialog.innerHTML.includes('Напоминать до ответа'));
  assert(dialog.innerHTML.includes('id="task-repeat" type="checkbox" >'));
  panel.data.tasks[0].repeat_until_ack=true;panel.edit(panel.data.tasks[0]);
  assert(dialog.innerHTML.includes('id="task-repeat" type="checkbox" checked'));
  assert(panel.tags(panel.data.tasks[0]).includes('До ответа'));
});
test('daily board and repeat interval are configurable',()=>{
  const panel=new Panel();panel.data=data();panel.data.repeat_interval_minutes=15;panel.settings();
  assert(dialog.innerHTML.includes('Дела на сегодня'));
  assert(dialog.innerHTML.includes('id="repeat-interval"'));
  assert(dialog.innerHTML.includes('value="15"'));
});


test('day-only plans span dates without inventing a time and appear in the editor',()=>{
  const task={id:'3',title:'Магазин',status:'active',due_at:null,due_date:'2026-11-11',end_date:'2026-11-13'};
  assert(utils.onDay(task,'2026-11-11','Europe/Moscow'));assert(utils.onDay(task,'2026-11-12','Europe/Moscow'));assert(!utils.onDay(task,'2026-11-13','Europe/Moscow'));
  const panel=new Panel();panel.data=data();panel.data.tasks.push(task);panel.month=new Date(2026,10,1);panel._hass={};panel.render();
  assert(app.innerHTML.includes('Весь день'));assert(panel.item(task).includes('В течение дня'));
  panel.edit(task);assert(dialog.innerHTML.includes('id="task-all-day" type="checkbox" checked'));assert(dialog.innerHTML.includes('value="2026-11-11"'));
});
test('Google direction, connection and first reminder are configurable',()=>{
  const panel=new Panel();panel.data=data();panel.data.google={calendars:[{value:'calendar.work',label:'Дела'}],calendar:'calendar.work',mode:'bidirectional',status:'ready'};panel.settings();
  assert(dialog.innerHTML.includes('value="calendar.work" selected'));assert(dialog.innerHTML.includes('value="bidirectional" selected'));assert(dialog.innerHTML.includes('Синхронизировать сейчас'));assert(dialog.innerHTML.includes('day-clock'));assert(dialog.innerHTML.includes('после первого уведомления'));
});

test('completed task exposes automatic reason and reply completion help',()=>{
  const panel=new Panel();panel.data=data();
  const task={...panel.data.tasks[0],status:'done',completion_reason:'elapsed'};
  assert(panel.tags(task).includes('Завершено по времени'));
  panel.edit(task);assert(dialog.innerHTML.includes('Ответ на напоминание завершает дело'));
  assert(!dialog.innerHTML.includes('Подтверждение не отмечает'));
});
test('moving completed task from editor reopens it atomically with new date',async()=>{
  const panel=new Panel();panel.data=data();panel.editing={...panel.data.tasks[0],status:'done',revision:3};
  const fields={'#task-title':{value:'Перенесённое дело'},'#task-notes':{value:''},'#task-date':{value:'2026-11-14'},'#task-time':{value:'18:00'},'#task-all-day':{checked:false},'#task-category':{value:'Работа'},'#task-priority':{value:'Обычная'},'#task-repeat':{checked:false},'.modalerror':{textContent:''}};
  const previous=dialog.querySelector;dialog.querySelector=selector=>fields[selector];
  let request;panel.call=async(type,payload)=>{request=payload.operation;};
  try{await panel.saveTask();assert.equal(request.action,'reopen');assert.equal(request.due_at,'2026-11-14T18:00');assert.equal(request.task_id,panel.editing.id);assert.equal(request.revision,3);}
  finally{dialog.querySelector=previous;}
});
