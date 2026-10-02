"use strict";

const SpeedBrowser = (() => {
  const CHUNK=65536, WINDOW=2097152, ACK_STEP=262144;
  const PROFILE={version:1,streams:4,warmup_seconds:2,measurement_seconds:10,timeout_seconds:60,setup_seconds:10,channel_seconds:5,idle_seconds:5,idle_samples:20,idle_interval:.1,loaded_interval:.2,rtt_timeout:2,liveness_seconds:5,chunk_bytes:CHUNK,window_bytes:WINDOW};
  const clock=()=>performance.now()/1000;
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const integer=value=>Number.isSafeInteger(value)&&value>=0;
  const failure=(code,message)=>Object.assign(new Error(message),{code});
  const nonce=()=>Array.from(crypto.getRandomValues(new Uint8Array(12)),b=>b.toString(16).padStart(2,"0")).join("");
  function summary(samples,timeouts=0,discarded=0) {
    const sorted=[...samples].sort((a,b)=>a-b),n=sorted.length;
    return {samples:n,median_ms:n?(sorted[Math.floor((n-1)/2)]+sorted[Math.floor(n/2)])/2:null,min_ms:n?sorted[0]:null,max_ms:n?sorted[n-1]:null,timeouts,discarded,timer_limited:sorted.includes(0)};
  }
  class Meter {
    constructor(profile=PROFILE,now=clock()) {this.profile=profile;this.begin=now+profile.warmup_seconds;this.end=this.begin+profile.measurement_seconds;this.buckets=Array(Math.ceil(profile.measurement_seconds)).fill(0);this.total=0;this.warm=0;}
    add(size,now=clock()) {this.total+=size;if(now<this.begin)this.warm+=size;else if(now<this.end)this.buckets[Math.min(this.buckets.length-1,Math.floor(now-this.begin))]+=size;}
    snapshot(now=clock(),complete=false) {
      const elapsed=Math.max(0,Math.min(this.profile.measurement_seconds,now-this.begin)),bytes=this.buckets.reduce((a,b)=>a+b,0),index=Math.min(this.buckets.length-1,Math.floor(elapsed)-1);
      complete=complete&&now>=this.end;
      return {complete,measurement_seconds:complete?this.profile.measurement_seconds:elapsed,received_bytes:bytes,warmup_bytes:this.warm,total_bytes:this.total,bucket_bytes:[...this.buckets],mbps:complete?bytes*8/this.profile.measurement_seconds/1e6:null,current_mbps:index>=0?this.buckets[index]*8/Math.min(1,this.profile.measurement_seconds-index)/1e6:null};
    }
  }
  function metric(value,profile=PROFILE,sent=Number.MAX_SAFE_INTEGER) {
    if(!value||value.complete!==true||Math.abs(value.measurement_seconds-profile.measurement_seconds)>1e-6||!Number.isFinite(value.measurement_seconds))throw failure("invalid_measurement","Receiver did not confirm a complete measurement window");
    if(![value.received_bytes,value.warmup_bytes,value.total_bytes].every(integer)||value.total_bytes>sent||value.received_bytes+value.warmup_bytes>value.total_bytes)throw failure("protocol_error","Invalid receiver byte counts");
    if(!Array.isArray(value.bucket_bytes)||value.bucket_bytes.length!==Math.ceil(profile.measurement_seconds)||!value.bucket_bytes.every(integer)||value.bucket_bytes.reduce((a,b)=>a+b,0)!==value.received_bytes)throw failure("protocol_error","Invalid receiver time buckets");
    return {complete:true,measurement_seconds:profile.measurement_seconds,received_bytes:value.received_bytes,warmup_bytes:value.warmup_bytes,total_bytes:value.total_bytes,bucket_bytes:[...value.bucket_bytes],mbps:value.received_bytes*8/profile.measurement_seconds/1e6};
  }
  function profileValid(profile) {return Object.keys(PROFILE).every(key=>profile?.[key]===PROFILE[key])&&Object.keys(profile).length===Object.keys(PROFILE).length;}
  class Channel {
    constructor(url,hello) {
      this.ws=new WebSocket(url);this.ws.binaryType="arraybuffer";this.closing=false;this.failure=null;this.handler=null;
      this.ready=new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{this.close();reject(failure("channel_timeout","Channel setup timed out"));},5000);
        this.ws.addEventListener("open",()=>{
          if(this.ws.extensions){clearTimeout(timer);this.close();reject(failure("compression","Compressed channels cannot be measured"));return;}
          this.ws.send(JSON.stringify(hello));
        });
        let ready=false;
        this.ws.addEventListener("message",event=>{
          if(this.closing||this.failure)return;
          try {
            if(!ready) {
              const message=JSON.parse(event.data);
              if(message.type!=="ready"||message.run_id!==hello.run_id||message.protocol!==1)throw failure("protocol_error","Channel authorization was rejected");
              ready=true;clearTimeout(timer);resolve(message);
            } else this.handler?.(event.data);
          }catch(error){this.failure=error.code?error:failure("protocol_error","Invalid speedtest channel message");if(!ready){clearTimeout(timer);reject(this.failure);}this.close(false);}
        });
        this.ws.addEventListener("error",()=>{this.failure=failure("connection_lost","A speedtest connection failed");clearTimeout(timer);reject(this.failure);});
        this.ws.addEventListener("close",event=>{if(!this.closing)this.failure ||= failure(event.code===4001?"cancelled":"connection_lost",event.code===4001?"Node canceled the test":"A speedtest connection closed early");clearTimeout(timer);reject(this.failure||failure("cancelled","Test stopped"));this.onclose?.();});
      });
    }
    check(){if(this.failure)throw this.failure;if(this.ws.readyState!==WebSocket.OPEN)throw failure("connection_lost","Test connection is not open");}
    send(value){this.check();this.ws.send(JSON.stringify(value));}
    close(expected=true){this.closing=expected;this.ws.close();}
  }
  class Control {
    constructor(channel) {
      this.channel=channel;this.pending=new Map();this.sequence=0;this.failure=null;this.windowStart=false;this.windowEnd=false;this.progress=()=>{};
      channel.handler=data=>{
        if(typeof data!=="string"||data.length>32768)throw failure("protocol_error","Invalid control frame");
        const value=JSON.parse(data);
        if(value.id!==undefined){const p=this.pending.get(value.id);if(p){clearTimeout(p.timer);this.pending.delete(value.id);p.resolve(value.data);}}
        else if(value.event==="window"&&value.direction==="upload"){if(value.state==="start")this.windowStart=true;if(value.state==="end")this.windowEnd=true;}
        else if(value.event==="progress")this.progress(value.phase,value.live);
        else if(value.event==="terminal"){
          const finishing=[...this.pending.entries()].find(([key,p])=>p.op==="finish");
          if(finishing){const[key,p]=finishing;clearTimeout(p.timer);this.pending.delete(key);p.resolve({metrics:value.result?.metrics,outcome:value.result?.outcome});}
          else if(value.result?.outcome!=="completed")this.failure=failure(value.result?.outcome==="cancelled"?"cancelled":"peer_stopped",value.result?.error?.message||"Node stopped the test");
        }
        else if(value.error)this.failure=failure("protocol_error","Node rejected the control operation");
      };
      channel.onclose=()=>{for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(this.failure||channel.failure||failure("connection_lost","Control connection closed"));}this.pending.clear();};
      this.heartbeat=setInterval(()=>{this.call("keepalive",{},2000).catch(error=>this.failure ||= error);},1000);
    }
    check(){if(this.failure)throw this.failure;this.channel.check();}
    call(op,data={},timeout=3000) {
      this.check();if(this.pending.size>=8)throw failure("protocol_error","Too many control operations");
      const id=++this.sequence;
      return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{this.pending.delete(id);reject(failure("rtt_timeout","Control reply timed out"));},timeout);
        this.pending.set(id,{resolve,reject,timer,op});
        try{this.channel.send({id,op,data});}catch(error){clearTimeout(timer);this.pending.delete(id);reject(error);}
      });
    }
    close(){clearInterval(this.heartbeat);this.channel.close();for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(failure("cancelled","Test stopped"));}this.pending.clear();}
  }
  class Latency {
    constructor(control,profile,end,upload=false){this.control=control;this.profile=profile;this.end=end;this.upload=upload;this.samples=[];this.timeouts=0;this.discarded=0;this.stopped=false;this.inflight=false;this.accounted=false;}
    async measure(idle=false,start=clock()) {
      if(start>clock())await sleep((start-clock())*1000);
      let attempts=0;
      while(!this.stopped&&clock()<this.end&&attempts++<(idle?this.profile.idle_samples:100)){
        const began=clock(),token=nonce();
        this.inflight=true;this.accounted=false;
        try{
          const reply=await this.control.call("echo",{nonce:token},this.profile.rtt_timeout*1000),ended=clock();
          if(reply?.nonce!==token)throw failure("protocol_error","Echo response did not match");
          let valid=!this.stopped&&ended<=this.end;
          if(this.upload)valid=valid&&reply.window==="upload"&&Number.isFinite(reply.remaining_seconds)&&reply.remaining_seconds>=ended-began;
          if(ended-began>this.profile.rtt_timeout){if(!this.accounted)this.timeouts++;}else if(valid)this.samples.push((ended-began)*1000);else if(!this.accounted)this.discarded++;
        }catch(error){if(error.code==="rtt_timeout"){if(!this.accounted)this.timeouts++;}else if(!this.stopped)throw error;}
        finally{this.inflight=false;}
        await sleep(Math.max(0,((idle?this.profile.idle_interval:this.profile.loaded_interval)-(clock()-began))*1000));
      }
      return this.value();
    }
    value(){return summary(this.samples,this.timeouts,this.discarded);}
    stop(){this.stopped=true;if(this.inflight&&!this.accounted){this.discarded++;this.accounted=true;}return this.value();}
  }
  class Transfers {
    constructor(direction,profile){this.direction=direction;this.profile=profile;this.channels=[];this.senders=[];this.meter=null;this.closing=false;}
    async open(connect) {
      for(let index=0;index<this.profile.streams;index++){
        const channel=await connect("data",this.direction,index);this.channels.push(channel);
        if(this.direction==="download"){
          let total=0,acked=0;
          channel.handler=data=>{
            if(!(data instanceof ArrayBuffer)||data.byteLength!==CHUNK||!this.meter)throw failure("protocol_error","Unexpected download payload");
            if(this.closing)return;
            this.meter.add(data.byteLength);total+=data.byteLength;
            if(total-acked>=ACK_STEP){acked=total;channel.send({ack:total});}
          };
        } else {
          const sender={channel,payload:crypto.getRandomValues(new Uint8Array(CHUNK)),sent:0,acked:0,running:false};this.senders.push(sender);
          channel.handler=data=>{
            if(typeof data!=="string"||data.length>128)throw failure("protocol_error","Invalid upload acknowledgement");
            const value=JSON.parse(data);
            if(!integer(value.ack)||value.ack<sender.acked||value.ack>sender.sent)throw failure("protocol_error","Invalid flow-control credit");
            sender.acked=value.ack;this.pump(sender);
          };
        }
      }
    }
    pump(sender){while(sender.running&&!this.closing&&sender.sent-sender.acked<WINDOW){sender.channel.check();if(sender.channel.ws.bufferedAmount>WINDOW)throw failure("buffer_limit","Sender queue exceeded its bound");sender.sent+=CHUNK;sender.channel.ws.send(sender.payload);}}
    upload(){for(const sender of this.senders){sender.running=true;this.pump(sender);}}
    check(){for(const channel of this.channels)channel.check();}
    stopSending(){this.closing=true;for(const sender of this.senders)sender.running=false;for(const channel of this.channels)channel.closing=true;}
    close(){this.stopSending();for(const channel of this.channels)channel.close();}
  }
  class Runner {
    constructor(accepted,api,csrf,progress) {
      this.result=structuredClone(accepted);this.api=api;this.csrf=csrf;this.progress=progress;this.control=null;this.data=null;this.latency=null;this.active=true;this.aborted=false;
      this.started=clock();this.wall=Date.now();this.profile=accepted.profile;this.channels=new Set();
      this.deadline=setTimeout(()=>this.abortLocal("timeout"),this.profile.timeout_seconds*1000);
    }
    check(){if(this.aborted)throw failure(this.abortReason||"cancelled","Test stopped");const elapsed=clock()-this.started,wall=(Date.now()-this.wall)/1000;if(Math.abs(elapsed-wall)>5)throw failure("timing_invalid","Clock discontinuity invalidated the test");if(elapsed>this.profile.timeout_seconds||wall>this.profile.timeout_seconds)throw failure("timeout","Overall deadline exceeded");this.control?.check();this.data?.check();}
    emit(){this.result.elapsed_seconds=clock()-this.started;this.progress(structuredClone(this.result));}
    async connect(role,direction,index) {
      const url=new URL("api/v1/speedtest/channel",location.href);url.protocol=url.protocol==="https:"?"wss:":"ws:";
      const hello={protocol:1,run_id:this.result.id,csrf:this.csrf,channel:role};if(role==="data")Object.assign(hello,{direction,index});
      const channel=new Channel(url,hello);this.channels.add(channel);
      const ready=await channel.ready;
      if(ready.instance!==this.result.responder_instance)throw failure("protocol_error","Node instance changed");
      return channel;
    }
    async execute() {
      try {
        if(!profileValid(this.profile))throw failure("profile_unsupported","This browser client does not support the node's test profile");
        this.control=new Control(await this.connect("control"));
        this.control.progress=(phase,live)=>{if(phase==="upload"||phase==="upload_warmup"){if(live&&(![live.received_bytes,live.warmup_bytes,live.total_bytes].every(integer)||!Number.isFinite(live.measurement_seconds)||live.measurement_seconds<0||live.measurement_seconds>10||(live.current_mbps!==null&&(!Number.isFinite(live.current_mbps)||live.current_mbps<0))))throw failure("protocol_error","Invalid receiver progress");this.result.phase=phase;this.result.live=live;this.emit();}};
        await this.control.call("echo",{nonce:"warm-control"});
        this.result.phase="idle";this.emit();
        const idle=new Latency(this.control,this.profile,clock()+this.profile.idle_seconds);
        this.result.metrics.idle_rtt=await idle.measure(true);
        await this.control.call("idle_done",{rtt:idle.value()});
        for(const direction of ["download","upload"]){
          this.check();this.result.phase=direction+"_setup";this.result.live=null;this.emit();
          const setup=clock();await this.control.call("prepare",{direction});
          this.data=new Transfers(direction,this.profile);await this.data.open((...args)=>this.connect(...args));
          if(clock()-setup>this.profile.channel_seconds)throw failure("channel_timeout","Payload setup timed out");
          if(direction==="download"){
            const meter=new Meter(this.profile);this.data.meter=meter;
            await this.control.call("go");
            this.latency=new Latency(this.control,this.profile,meter.end);
            const latencyTask=this.latency.measure(false,meter.begin);latencyTask.catch(()=>{});
            let next=0;
            while(clock()<meter.end){this.check();if(clock()>=next){this.result.phase=clock()>=meter.begin?"download":"download_warmup";this.result.live=meter.snapshot();this.emit();next=clock()+1;}await sleep(20);}
            this.check();this.latency.stop();
            this.result.metrics.download=metric(meter.snapshot(clock(),true),this.profile);
            const rtt=this.latency.value();this.result.metrics.download_rtt=rtt;
            this.data.stopSending();
            await this.control.call("stop_download",{metric:this.result.metrics.download,rtt});
            await latencyTask;this.latency=null;
          } else {
            await this.control.call("go");this.data.upload();
            while(!this.control.windowStart){this.check();await sleep(20);}
            this.latency=new Latency(this.control,this.profile,clock()+this.profile.measurement_seconds,true);
            const latencyTask=this.latency.measure();latencyTask.catch(()=>{});
            while(!this.control.windowEnd){this.check();await sleep(20);}
            this.check();this.latency.stop();
            const rtt=this.latency.value();this.result.metrics.upload_rtt=rtt;
            this.data.stopSending();
            const reply=await this.control.call("stop_upload",{rtt});
            this.result.metrics.upload=metric(reply.metric,this.profile,this.data.senders.reduce((n,s)=>n+s.sent,0));
            await latencyTask;this.latency=null;
          }
          this.data.close();this.data=null;this.result.live=null;this.emit();
        }
        this.result.phase="finalizing";this.emit();const final=await this.control.call("finish");
        if(final.outcome==="cancelled")throw failure("cancelled","Node canceled the test before completion");
        if(!["completed","failed"].includes(final.outcome))throw failure("protocol_error","Missing terminal outcome");
        const valid=["idle_rtt","download_rtt","upload_rtt"].every(key=>this.result.metrics[key]?.samples>0);
        this.result.outcome=valid&&final.outcome==="completed"?"completed":"failed";this.result.error=this.result.outcome==="completed"?null:{code:"latency_unavailable",message:"A required measurement could not be finalized; valid speed measurements are retained."};
      }catch(error){
        if(this.cancelRequested)error=failure("cancelled","Test canceled");
        this.result.outcome=error.code==="cancelled"||this.abortReason==="cancelled"?"cancelled":"failed";
        this.result.error={code:error.code||"connection_lost",message:error.message||"The browser could not complete this test"};
        try{await this.api(`/speedtest/runs/${this.result.id}`,{method:"DELETE",body:"{}"});}catch{}
      }finally{
        clearTimeout(this.deadline);if(this.latency)this.latency.stop();
        this.data?.close();this.control?.close();for(const channel of this.channels)channel.close();this.channels.clear();this.data=null;this.control=null;this.latency=null;this.csrf="";
        this.result.elapsed_seconds=clock()-this.started;this.result.completed_at=new Date(Date.parse(this.result.started_at)+this.result.elapsed_seconds*1000).toISOString();this.result.phase="finished";this.result.live=null;this.active=false;this.emit();
      }
      return this.result;
    }
    abortLocal(reason="cancelled"){this.aborted=true;this.abortReason=reason;this.data?.close();this.control?.close();for(const channel of this.channels)channel.close();}
    async cancel(){this.cancelRequested=true;try{await this.api(`/speedtest/runs/${this.result.id}`,{method:"DELETE",body:"{}"});}catch(error){this.cancelRequested=false;throw error;}}
  }
  return {PROFILE,Meter,metric,summary,profileValid,Runner};
})();

if(typeof module!=="undefined"&&module.exports)module.exports=SpeedBrowser;
