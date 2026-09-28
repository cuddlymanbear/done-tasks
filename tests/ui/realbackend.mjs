import fs from 'node:fs'
import { PLUGIN, fixture, out, requireFromHost as req } from './paths.mjs'

// Run: node --import ./loader-hook.mjs realbackend.mjs   (the hook maps @hermes/plugin-sdk)
const React=req('react'), jsxRuntime=req('react/jsx-runtime')
const {renderToStaticMarkup}=req('react-dom/server')
const S={}; Object.assign(S,React,jsxRuntime); S.cn=(...a)=>a.filter(Boolean).join(' ')
S.Badge=({children,variant,...p})=>React.createElement('span',{'data-badge':variant||'default',...p},children)
S.Button=({children,onClick,disabled,title,size,variant,className,...p})=>React.createElement('button',{onClick,disabled,title,'data-size':size,className,...p},children)
S.Codicon=({name,className})=>React.createElement('i',{className})
S.EmptyState=({title,description})=>React.createElement('div',{'data-empty':title},title,description)
S.ErrorState=({title,description})=>React.createElement('div',{'data-error':title},title,description)
S.Skeleton=({className})=>React.createElement('div',{className})
S.ScrollArea=({children,className})=>React.createElement('div',{className},children)
S.SegmentedControl=({options,value,onChange})=>React.createElement('div',{'data-segmented':value},(options||[]).map(o=>React.createElement('button',{key:o.id,'data-opt':o.id},o.label)))
S.Tip=({label,children})=>React.cloneElement(children,{'data-tip':String(label)})
S.host={}
S.useQuery=(o)=>{globalThis.__Q=o; return globalThis.__STATE}
S.ROUTES_AREA='routes'; S.SIDEBAR_NAV_AREA='sidebar.nav'
globalThis.__DT_SDK__=S
globalThis.window={localStorage:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}}

const mod=await import('file://'+PLUGIN)
let page
mod.default.register({register:()=>()=>{},registerMany:(cs)=>{page=cs.find(c=>c.area==='routes');return()=>{}},rest:async()=>({})})

const real=fixture('real-done.json')
// Feed the REAL backend payload straight into the panel's normaliser via the
// documented /done response shape.
globalThis.__STATE={isLoading:false,isError:false,data:{items:real.items,total:real.total,degraded:false},error:null,refetch:()=>{}}
globalThis.__REST=async()=>real
const html=renderToStaticMarkup(React.createElement(page.render))
fs.writeFileSync(out('real-backend.html'),html)
const rows=(html.match(/<li/g)||[]).length
console.log('REAL BACKEND PAYLOAD -> PANEL')
console.log('backend items      :', real.items.length, '| total', real.total)
console.log('rows rendered      :', rows)
console.log('first task title   :', real.items[0].title.slice(0,50))
console.log('title rendered     :', html.includes(real.items[0].title.slice(0,40)))
console.log('pending note shown :', html.includes('Summary still being written'))
console.log('gap banner absent  :', !html.includes('has not shipped yet'))
console.log('rows==items        :', rows===real.items.length)
process.exit(rows===real.items.length && html.includes(real.items[0].title.slice(0,40)) ? 0:1)
