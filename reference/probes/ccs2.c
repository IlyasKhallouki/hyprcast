#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <va/va_vpp.h>
#include <drm_fourcc.h>
#include <stdlib.h>
static const uint64_t M[]={0,0x0100000000000001ull,0x0100000000000002ull,0x0100000000000004ull};
static const char*N[]={"LINEAR","X_TILED","Y_TILED","Y_TILED_CCS"};
int W=1920,H=1080;
int main(void){
  int fd=open("/dev/dri/renderD128",O_RDWR|O_CLOEXEC);
  struct gbm_device*g=gbm_create_device(fd);
  VADisplay d=vaGetDisplayDRM(fd); int a,b; vaInitialize(d,&a,&b);
  printf("driver: %s\n",vaQueryVendorString(d));
  VASurfaceAttrib na={VASurfaceAttribPixelFormat,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_FOURCC_NV12}}};
  VASurfaceID nv12; vaCreateSurfaces(d,VA_RT_FORMAT_YUV420,W,H,&nv12,1,&na,1);
  VAConfigID vcfg; VAContextID vctx;
  vaCreateConfig(d,VAProfileNone,VAEntrypointVideoProc,NULL,0,&vcfg);
  vaCreateContext(d,vcfg,W,H,VA_PROGRESSIVE,&nv12,1,&vctx);
  for(int i=0;i<4;i++){
    struct gbm_bo*bo=gbm_bo_create_with_modifiers2(g,W,H,GBM_FORMAT_XRGB8888,&M[i],1,GBM_BO_USE_RENDERING);
    if(!bo){printf("%-12s: alloc failed\n",N[i]);continue;}
    int np=gbm_bo_get_plane_count(bo);
    VADRMPRIMESurfaceDescriptor de; memset(&de,0,sizeof de);
    de.fourcc=VA_FOURCC_BGRX; de.width=W; de.height=H;
    de.num_objects=1; de.objects[0].fd=gbm_bo_get_fd_for_plane(bo,0);
    de.objects[0].size=(uint32_t)lseek(de.objects[0].fd,0,SEEK_END);
    de.objects[0].drm_format_modifier=gbm_bo_get_modifier(bo);
    de.num_layers=1; de.layers[0].drm_format=DRM_FORMAT_XRGB8888; de.layers[0].num_planes=np;
    for(int p=0;p<np;p++){de.layers[0].object_index[p]=0;de.layers[0].offset[p]=gbm_bo_get_offset(bo,p);
      de.layers[0].pitch[p]=gbm_bo_get_stride_for_plane(bo,p);}
    VASurfaceAttrib at[2]={{VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
      {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&de}}}};
    VASurfaceID s; VAStatus st=vaCreateSurfaces(d,VA_RT_FORMAT_RGB32,W,H,&s,1,at,2);
    printf("%-12s planes=%d objsize=%u  import=%s",N[i],np,de.objects[0].size,st==VA_STATUS_SUCCESS?"OK":vaErrorStr(st));
    if(st==VA_STATUS_SUCCESS){
      VAProcPipelineParameterBuffer p; memset(&p,0,sizeof p);
      VARectangle r={0,0,W,H};
      p.surface=s;p.surface_region=&r;p.output_region=&r;
      p.surface_color_standard=VAProcColorStandardNone;p.output_color_standard=VAProcColorStandardBT709;
      p.filter_flags=VA_FRAME_PICTURE|VA_FILTER_SCALING_FAST;
      VABufferID pb; vaCreateBuffer(d,vctx,VAProcPipelineParameterBufferType,sizeof p,1,&p,&pb);
      VAStatus v1=vaBeginPicture(d,vctx,nv12), v2=vaRenderPicture(d,vctx,&pb,1), v3=vaEndPicture(d,vctx), v4=vaSyncSurface(d,nv12);
      printf("  VPP=%s", (v1||v2||v3||v4)?vaErrorStr(v1?v1:v2?v2:v3?v3:v4):"OK");
      vaDestroyBuffer(d,pb);
    }
    printf("\n");
  }
  return 0;
}
