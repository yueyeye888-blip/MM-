#!/usr/bin/env node

const {spawn}=require('node:child_process');
const path=require('node:path');

const projectDir=path.resolve(__dirname,'..');
const pythonBin=process.env.MM_MONITOR_PYTHON||'/usr/bin/python3';
const args=[
  '-m','uvicorn','app.main:app',
  '--host','127.0.0.1','--port','8765',
  '--loop','asyncio','--http','h11','--ws','none',
];
const child=spawn(pythonBin,args,{
  cwd:projectDir,
  env:{...process.env,PYTHONUNBUFFERED:'1'},
  stdio:'inherit',
});

let stopping=false;
function stop(signal){
  if(stopping)return;
  stopping=true;
  if(!child.killed)child.kill(signal);
}
process.on('SIGTERM',()=>stop('SIGTERM'));
process.on('SIGINT',()=>stop('SIGINT'));
child.on('error',error=>{
  console.error(`Agent 启动失败：${error.message}`);
  process.exitCode=1;
});
child.on('exit',(code,signal)=>{
  if(signal)process.kill(process.pid,signal);
  else process.exit(code??1);
});
