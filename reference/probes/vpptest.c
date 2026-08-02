#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_str.h>
#include <va/va_vpp.h>
#include <va/va_drmcommon.h>
static uint64_t ns(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec*1000000000ull+t.tv_nsec;}
#define CK(x,m) do{VAStatus _s=(x); if(_s!=VA_STATUS_SUCCESS){printf("FAIL %s: %s\n",m,vaErrorStr(_s));return 1;}}while(0)
int main(void){
  int fd=open("/dev/dri/renderD128",O_RDWR|O_CLOEXEC);
  struct gbm_device *g=gbm_create_device(fd);
  VADisplay va=vaGetDisplayDRM(fd); int mj,mn;
  CK(vaInitialize(va,&mj,&mn),"init");
  // 1. import Y_TILED XRGB dmabuf (what Hyprland will fill)
  uint64_t mod=0x0100000000000002ull;
  struct gbm_bo *bo=gbm_bo_create_with_modifiers2(g,1920,1080,GBM_FORMAT_XRGB8888,&mod,1,GBM_BO_USE_RENDERING);
  VADRMPRIMESurfaceDescriptor d; memset(&d,0,sizeof d);
  d.fourcc=VA_FOURCC_BGRX; d.width=1920; d.height=1080; d.num_objects=1;
  d.objects[0].fd=gbm_bo_get_fd_for_plane(bo,0);
  d.objects[0].size=gbm_bo_get_stride_for_plane(bo,0)*1080;
  d.objects[0].drm_format_modifier=gbm_bo_get_modifier(bo);
  d.num_layers=1; d.layers[0].drm_format=GBM_FORMAT_XRGB8888; d.layers[0].num_planes=1;
  d.layers[0].object_index[0]=0; d.layers[0].offset[0]=0; d.layers[0].pitch[0]=gbm_bo_get_stride_for_plane(bo,0);
  VASurfaceAttrib at[2]={{VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
                         {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&d}}}};
  VASurfaceID rgb; CK(vaCreateSurfaces(va,VA_RT_FORMAT_RGB32,1920,1080,&rgb,1,at,2),"import RGB");
  printf("imported Y_TILED BGRX surface OK\n");
  // 2. NV12 destination (driver-allocated)
  VASurfaceID nv12; CK(vaCreateSurfaces(va,VA_RT_FORMAT_YUV420,1920,1080,&nv12,1,NULL,0),"alloc NV12");
  printf("allocated NV12 surface OK\n");
  // 3. VPP context
  VAConfigID cfg; CK(vaCreateConfig(va,VAProfileNone,VAEntrypointVideoProc,NULL,0,&cfg),"vpp config");
  VAContextID ctx; CK(vaCreateContext(va,cfg,1920,1080,VA_PROGRESSIVE,&nv12,1,&ctx),"vpp ctx");
  printf("VPP context created OK\n");
  // 4. run N conversions, time them
  int N=100; uint64_t t0=ns();
  for(int i=0;i<N;i++){
    VAProcPipelineParameterBuffer p; memset(&p,0,sizeof p);
    VARectangle r={0,0,1920,1080};
    p.surface=rgb; p.surface_region=&r; p.output_region=&r;
    p.filter_flags=VA_FILTER_SCALING_FAST;
    p.surface_color_standard=VAProcColorStandardNone;
    p.output_color_standard=VAProcColorStandardBT601;
    VABufferID pb; CK(vaCreateBuffer(va,ctx,VAProcPipelineParameterBufferType,sizeof p,1,&p,&pb),"buf");
    CK(vaBeginPicture(va,ctx,nv12),"begin");
    CK(vaRenderPicture(va,ctx,&pb,1),"render");
    CK(vaEndPicture(va,ctx),"end");
    CK(vaSyncSurface(va,nv12),"sync");
    vaDestroyBuffer(va,pb);
  }
  uint64_t dt=ns()-t0;
  printf("\nVPP BGRX(Y_TILED,imported dmabuf) -> NV12: %d convs in %.1fms = %.3f ms/frame (%.0f fps ceiling)\n",
         N, dt/1e6, dt/1e6/N, N/(dt/1e9));
  // 5. confirm NV12 export works (what the encoder consumes)
  VADRMPRIMESurfaceDescriptor od; memset(&od,0,sizeof od);
  VAStatus s=vaExportSurfaceHandle(va,nv12,VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2,VA_EXPORT_SURFACE_READ_ONLY,&od);
  printf("NV12 export: %s fourcc=%.4s objects=%u layers=%u mod=0x%016lx\n",
    s==VA_STATUS_SUCCESS?"OK":vaErrorStr(s),(char*)&od.fourcc,od.num_objects,od.num_layers,
    (unsigned long)od.objects[0].drm_format_modifier);
  return 0;
}
