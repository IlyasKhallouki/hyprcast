#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <drm_fourcc.h>
int main(void){
  int fd=open("/dev/dri/renderD128",O_RDWR|O_CLOEXEC);
  struct gbm_device*g=gbm_create_device(fd);
  VADisplay d=vaGetDisplayDRM(fd); int a,b; vaInitialize(d,&a,&b);
  uint64_t mod=0x0100000000000004ull;
  struct gbm_bo*bo=gbm_bo_create_with_modifiers2(g,1920,1080,GBM_FORMAT_XRGB8888,&mod,1,GBM_BO_USE_RENDERING);
  int np=gbm_bo_get_plane_count(bo);
  printf("planes=%d\n",np);
  for(int p=0;p<np;p++){
    int pfd=gbm_bo_get_fd_for_plane(bo,p);
    printf(" plane%d off=%u pitch=%u fd=%d lseek=%ld\n",p,gbm_bo_get_offset(bo,p),gbm_bo_get_stride_for_plane(bo,p),pfd,(long)lseek(pfd,0,SEEK_END));
  }
  // try generous sizes
  uint32_t sizes[3]={8388608u,12582912u,16777216u};
  for(int k=0;k<3;k++){
    VADRMPRIMESurfaceDescriptor de; memset(&de,0,sizeof de);
    de.fourcc=VA_FOURCC_BGRX; de.width=1920; de.height=1080;
    de.num_objects=1; de.objects[0].fd=gbm_bo_get_fd_for_plane(bo,0);
    de.objects[0].size=sizes[k]; de.objects[0].drm_format_modifier=mod;
    de.num_layers=1; de.layers[0].drm_format=DRM_FORMAT_XRGB8888; de.layers[0].num_planes=np;
    for(int p=0;p<np;p++){de.layers[0].object_index[p]=0;de.layers[0].offset[p]=gbm_bo_get_offset(bo,p);de.layers[0].pitch[p]=gbm_bo_get_stride_for_plane(bo,p);}
    VASurfaceAttrib at[2]={{VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
      {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&de}}}};
    VASurfaceID s; VAStatus st=vaCreateSurfaces(d,VA_RT_FORMAT_RGB32,1920,1080,&s,1,at,2);
    printf("size=%u num_planes=%d -> %s\n",sizes[k],np,st==VA_STATUS_SUCCESS?"OK":vaErrorStr(st));
  }
  // try declaring only 1 plane (ignore CCS aux)
  {
    VADRMPRIMESurfaceDescriptor de; memset(&de,0,sizeof de);
    de.fourcc=VA_FOURCC_BGRX; de.width=1920; de.height=1080;
    de.num_objects=1; de.objects[0].fd=gbm_bo_get_fd_for_plane(bo,0);
    de.objects[0].size=8388608u; de.objects[0].drm_format_modifier=mod;
    de.num_layers=1; de.layers[0].drm_format=DRM_FORMAT_XRGB8888; de.layers[0].num_planes=1;
    de.layers[0].object_index[0]=0; de.layers[0].offset[0]=0; de.layers[0].pitch[0]=gbm_bo_get_stride_for_plane(bo,0);
    VASurfaceAttrib at[2]={{VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
      {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&de}}}};
    VASurfaceID s; VAStatus st=vaCreateSurfaces(d,VA_RT_FORMAT_RGB32,1920,1080,&s,1,at,2);
    printf("declare-1-plane (drop CCS aux) -> %s\n",st==VA_STATUS_SUCCESS?"OK (but compressed data misread!)":vaErrorStr(st));
  }
  return 0;
}
