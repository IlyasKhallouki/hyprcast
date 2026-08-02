#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <wayland-client.h>
#include "ext-image-capture-source-v1-client-protocol.h"
#include "ext-image-copy-capture-v1-client-protocol.h"
static struct ext_output_image_capture_source_manager_v1 *sm;
static struct ext_image_copy_capture_manager_v1 *cm;
static struct wl_output *out;
static int nconstraint, gotdone, gotstop;
static void bs(void*d,struct ext_image_copy_capture_session_v1*s,uint32_t w,uint32_t h){(void)d;(void)s;printf("  buffer_size %ux%u\n",w,h);nconstraint++;}
static void sf(void*d,struct ext_image_copy_capture_session_v1*s,uint32_t f){(void)d;(void)s;(void)f;nconstraint++;}
static void dd(void*d,struct ext_image_copy_capture_session_v1*s,struct wl_array*a){(void)d;(void)s;(void)a;nconstraint++;}
static void df(void*d,struct ext_image_copy_capture_session_v1*s,uint32_t f,struct wl_array*a){(void)d;(void)s;(void)f;(void)a;nconstraint++;}
static void dn(void*d,struct ext_image_copy_capture_session_v1*s){(void)d;(void)s;gotdone++;printf("  done\n");}
static void st(void*d,struct ext_image_copy_capture_session_v1*s){(void)d;(void)s;gotstop=1;printf("  STOPPED\n");}
static const struct ext_image_copy_capture_session_v1_listener L={bs,sf,dd,df,dn,st};
static void ga(void*d,struct wl_registry*r,uint32_t n,const char*i,uint32_t v){(void)d;
 if(!strcmp(i,"ext_output_image_capture_source_manager_v1"))sm=wl_registry_bind(r,n,&ext_output_image_capture_source_manager_v1_interface,1);
 else if(!strcmp(i,"ext_image_copy_capture_manager_v1"))cm=wl_registry_bind(r,n,&ext_image_copy_capture_manager_v1_interface,1);
 else if(!strcmp(i,"wl_output")&&!out)out=wl_registry_bind(r,n,&wl_output_interface,v>4?4:v);}
static void gr(void*d,struct wl_registry*r,uint32_t n){(void)d;(void)r;(void)n;}
static const struct wl_registry_listener RL={ga,gr};
int main(int argc,char**argv){
 uint32_t opt=argc>1?(uint32_t)strtoul(argv[1],0,0):1;
 struct wl_display*dp=wl_display_connect(NULL);
 struct wl_registry*rg=wl_display_get_registry(dp);
 wl_registry_add_listener(rg,&RL,NULL);
 wl_display_roundtrip(dp);wl_display_roundtrip(dp);
 printf("create_session options=0x%x\n",opt);
 struct ext_image_capture_source_v1*s=ext_output_image_capture_source_manager_v1_create_source(sm,out);
 struct ext_image_copy_capture_session_v1*se=ext_image_copy_capture_manager_v1_create_session(cm,s,opt);
 ext_image_copy_capture_session_v1_add_listener(se,&L,NULL);
 int r=wl_display_roundtrip(dp);
 printf("roundtrip=%d constraint_events=%d done=%d stopped=%d\n",r,nconstraint,gotdone,gotstop);
 if(r<0){int e=wl_display_get_error(dp);
   const struct wl_interface*ifc=NULL;uint32_t id=0,code=0;
   code=wl_display_get_protocol_error(dp,&ifc,&id);
   printf("display error errno=%d protocol_error code=%u iface=%s id=%u\n",e,code,ifc?ifc->name:"(none)",id);}
 return 0;}
