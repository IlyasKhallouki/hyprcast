#include <stdio.h>
#include <string.h>
#include <wayland-client.h>
static void ga(void*d,struct wl_registry*r,uint32_t n,const char*i,uint32_t v){(void)d;(void)r;
 if(strstr(i,"syncobj")||strstr(i,"screencopy")||strstr(i,"image_c")||strstr(i,"dmabuf")||strstr(i,"presentation")||strstr(i,"foreign_toplevel"))
   printf("%-58s v%-2u name=%u\n",i,v,n);}
static void gr(void*d,struct wl_registry*r,uint32_t n){(void)d;(void)r;(void)n;}
static const struct wl_registry_listener L={ga,gr};
int main(){struct wl_display*d=wl_display_connect(NULL);struct wl_registry*g=wl_display_get_registry(d);
 wl_registry_add_listener(g,&L,0);wl_display_roundtrip(d);wl_display_roundtrip(d);return 0;}
