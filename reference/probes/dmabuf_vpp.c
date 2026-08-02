// Prove: GBM dmabuf (XRGB8888) -> VAAPI import (PRIME_2) -> VPP RGB->NV12 -> H264 encode-ready
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <va/va_vpp.h>
#include <drm_fourcc.h>

#define CHK(x) do{VAStatus _s=(x); if(_s!=VA_STATUS_SUCCESS){printf("FAIL %s -> %s\n",#x,vaErrorStr(_s));return 1;}}while(0)
static double now(){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}

int W=1920,H=1080;

int main(){
  int fd=open("/dev/dri/renderD128",O_RDWR);
  struct gbm_device *gbm=gbm_create_device(fd);
  if(!gbm){printf("gbm_create_device failed\n");return 1;}
  printf("GBM backend: %s\n", gbm_device_get_backend_name(gbm));

  // Allocate a scanout-like BO, exactly what a compositor would hand us
  struct gbm_bo *bo=gbm_bo_create(gbm,W,H,GBM_FORMAT_XRGB8888,GBM_BO_USE_RENDERING|GBM_BO_USE_SCANOUT);
  if(!bo){printf("gbm_bo_create failed\n");return 1;}
  int nplanes=gbm_bo_get_plane_count(bo);
  uint64_t mod=gbm_bo_get_modifier(bo);
  printf("BO: %dx%d planes=%d modifier=0x%016lx stride=%u offset=%u\n",W,H,nplanes,(unsigned long)mod,
    gbm_bo_get_stride_for_plane(bo,0), gbm_bo_get_offset(bo,0));
  int dfd=gbm_bo_get_fd(bo);
  printf("dmabuf fd=%d\n",dfd);

  VADisplay d=vaGetDisplayDRM(fd);
  int maj,min; CHK(vaInitialize(d,&maj,&min));
  printf("driver: %s\n", vaQueryVendorString(d));

  // --- IMPORT via PRIME_2 ---
  VADRMPRIMESurfaceDescriptor desc; memset(&desc,0,sizeof(desc));
  desc.fourcc=VA_FOURCC_BGRX;   // XRGB8888 little-endian == BGRX byte order
  desc.width=W; desc.height=H;
  desc.num_objects=1;
  desc.objects[0].fd=dfd;
  desc.objects[0].size=gbm_bo_get_stride_for_plane(bo,0)*H;
  desc.objects[0].drm_format_modifier=mod;
  desc.num_layers=1;
  desc.layers[0].drm_format=DRM_FORMAT_XRGB8888;
  desc.layers[0].num_planes=1;
  desc.layers[0].object_index[0]=0;
  desc.layers[0].offset[0]=gbm_bo_get_offset(bo,0);
  desc.layers[0].pitch[0]=gbm_bo_get_stride_for_plane(bo,0);

  VASurfaceAttrib attrs[2];
  attrs[0].type=VASurfaceAttribMemoryType; attrs[0].flags=VA_SURFACE_ATTRIB_SETTABLE;
  attrs[0].value.type=VAGenericValueTypeInteger;
  attrs[0].value.value.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2;
  attrs[1].type=VASurfaceAttribExternalBufferDescriptor; attrs[1].flags=VA_SURFACE_ATTRIB_SETTABLE;
  attrs[1].value.type=VAGenericValueTypePointer; attrs[1].value.value.p=&desc;

  VASurfaceID rgbsurf;
  VAStatus s=vaCreateSurfaces(d,VA_RT_FORMAT_RGB32,W,H,&rgbsurf,1,attrs,2);
  if(s!=VA_STATUS_SUCCESS){printf("PRIME_2 IMPORT FAILED: %s\n",vaErrorStr(s));return 1;}
  printf("*** PRIME_2 import of XRGB8888 dmabuf: OK (surface 0x%x)\n",rgbsurf);

  // --- NV12 destination surface (internally allocated) ---
  VASurfaceAttrib na={.type=VASurfaceAttribPixelFormat,.flags=VA_SURFACE_ATTRIB_SETTABLE,
    .value={.type=VAGenericValueTypeInteger,.value={.i=VA_FOURCC_NV12}}};
  VASurfaceID nv12;
  CHK(vaCreateSurfaces(d,VA_RT_FORMAT_YUV420,W,H,&nv12,1,&na,1));
  printf("NV12 dst surface: OK\n");

  // --- VPP context ---
  VAConfigID vcfg; VAContextID vctx;
  CHK(vaCreateConfig(d,VAProfileNone,VAEntrypointVideoProc,NULL,0,&vcfg));
  CHK(vaCreateContext(d,vcfg,W,H,VA_PROGRESSIVE,&nv12,1,&vctx));
  printf("VPP context: OK\n");

  VABufferID pbuf;
  VAProcPipelineParameterBuffer p; 
  VARectangle r={0,0,W,H};
  double t0,t1; double best=1e9,sum=0; int N=120;
  for(int i=0;i<N;i++){
    memset(&p,0,sizeof(p));
    p.surface=rgbsurf;
    p.surface_region=&r; p.output_region=&r;
    p.surface_color_standard=VAProcColorStandardNone;   // sRGB in
    p.output_color_standard=VAProcColorStandardBT709;   // BT.709 out
    p.output_background_color=0xff000000;
    p.filter_flags=VA_FRAME_PICTURE|VA_FILTER_SCALING_FAST;
    p.filters=NULL; p.num_filters=0;
    CHK(vaCreateBuffer(d,vctx,VAProcPipelineParameterBufferType,sizeof(p),1,&p,&pbuf));
    t0=now();
    CHK(vaBeginPicture(d,vctx,nv12));
    CHK(vaRenderPicture(d,vctx,&pbuf,1));
    CHK(vaEndPicture(d,vctx));
    CHK(vaSyncSurface(d,nv12));
    t1=now();
    vaDestroyBuffer(d,pbuf);
    double ms=(t1-t0)*1000.0;
    if(i>=20){ if(ms<best)best=ms; sum+=ms; }
  }
  printf("*** VPP BGRX->NV12 %dx%d : best=%.3f ms  mean=%.3f ms over %d iters\n",W,H,best,sum/(N-20),N-20);

  // verify pixels actually landed
  VAImage img;
  if(vaDeriveImage(d,nv12,&img)==VA_STATUS_SUCCESS){
    printf("derived NV12 image: fourcc=%c%c%c%c pitch0=%u pitch1=%u planes=%u size=%u\n",
      img.format.fourcc&0xff,(img.format.fourcc>>8)&0xff,(img.format.fourcc>>16)&0xff,(img.format.fourcc>>24)&0xff,
      img.pitches[0],img.pitches[1],img.num_planes,img.data_size);
    vaDestroyImage(d,img.image_id);
  }

  // --- also confirm the encoder accepts this NV12 surface ---
  VAConfigAttrib ea={VAConfigAttribRTFormat,VA_RT_FORMAT_YUV420};
  VAConfigID ecfg; VAContextID ectx;
  if(vaCreateConfig(d,VAProfileH264ConstrainedBaseline,VAEntrypointEncSlice,&ea,1,&ecfg)==VA_STATUS_SUCCESS){
    if(vaCreateContext(d,ecfg,W,H,VA_PROGRESSIVE,&nv12,1,&ectx)==VA_STATUS_SUCCESS){
      printf("*** H264 ConstrainedBaseline EncSlice context on the VPP output surface: OK\n");
      vaDestroyContext(d,ectx);
    } else printf("enc context failed\n");
    vaDestroyConfig(d,ecfg);
  }
  // LP entrypoint too
  if(vaCreateConfig(d,VAProfileH264ConstrainedBaseline,VAEntrypointEncSliceLP,&ea,1,&ecfg)==VA_STATUS_SUCCESS){
    if(vaCreateContext(d,ecfg,W,H,VA_PROGRESSIVE,&nv12,1,&ectx)==VA_STATUS_SUCCESS){
      printf("*** H264 ConstrainedBaseline EncSliceLP context: OK\n");
      vaDestroyContext(d,ectx);
    }
    vaDestroyConfig(d,ecfg);
  }
  return 0;
}
