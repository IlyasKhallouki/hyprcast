#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <drm_fourcc.h>
int main(){
  int W=1920,H=1080;
  int fd=open("/dev/dri/renderD128",O_RDWR);
  struct gbm_device*g=gbm_create_device(fd);
  VADisplay d=vaGetDisplayDRM(fd); int a,b; vaInitialize(d,&a,&b);
  printf("driver: %s\n",vaQueryVendorString(d));
  uint64_t linear=DRM_FORMAT_MOD_LINEAR;
  struct { const char*name; struct gbm_bo*bo; } cases[2];
  cases[0].name="TILED (gbm_bo_create SCANOUT|RENDERING)";
  cases[0].bo=gbm_bo_create(g,W,H,GBM_FORMAT_XRGB8888,GBM_BO_USE_RENDERING|GBM_BO_USE_SCANOUT);
  cases[1].name="LINEAR (modifier list = LINEAR)";
  cases[1].bo=gbm_bo_create_with_modifiers(g,W,H,GBM_FORMAT_XRGB8888,&linear,1);
  for(int c=0;c<2;c++){
    if(!cases[c].bo){printf("%s: gbm alloc failed\n",cases[c].name);continue;}
    uint64_t mod=gbm_bo_get_modifier(cases[c].bo);
    unsigned stride=gbm_bo_get_stride_for_plane(cases[c].bo,0);
    printf("\n## %s  modifier=0x%016llx stride=%u\n",cases[c].name,(unsigned long long)mod,stride);
    int dfd=gbm_bo_get_fd(cases[c].bo);
    // --- legacy VASurfaceAttribExternalBuffers + MEM_TYPE_DRM_PRIME ---
    VASurfaceAttribExternalBuffers ext; memset(&ext,0,sizeof(ext));
    unsigned long h=dfd;
    ext.pixel_format=VA_FOURCC_BGRX; ext.width=W; ext.height=H;
    ext.data_size=stride*H; ext.num_planes=1; ext.pitches[0]=stride; ext.offsets[0]=0;
    ext.buffers=&h; ext.num_buffers=1; ext.flags=0;
    VASurfaceAttrib at[2]={
      {VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME}}},
      {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&ext}}}};
    VASurfaceID s; VAStatus st=vaCreateSurfaces(d,VA_RT_FORMAT_RGB32,W,H,&s,1,at,2);
    printf("   legacy ExternalBuffers/DRM_PRIME  : %s\n", st==VA_STATUS_SUCCESS?"OK":vaErrorStr(st));
    // --- PRIME_2 ---
    VADRMPRIMESurfaceDescriptor de; memset(&de,0,sizeof(de));
    de.fourcc=VA_FOURCC_BGRX; de.width=W; de.height=H; de.num_objects=1;
    de.objects[0].fd=dfd; de.objects[0].size=stride*H; de.objects[0].drm_format_modifier=mod;
    de.num_layers=1; de.layers[0].drm_format=DRM_FORMAT_XRGB8888; de.layers[0].num_planes=1;
    de.layers[0].object_index[0]=0; de.layers[0].offset[0]=0; de.layers[0].pitch[0]=stride;
    VASurfaceAttrib at2[2]={
      {VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
      {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&de}}}};
    VASurfaceID s2; st=vaCreateSurfaces(d,VA_RT_FORMAT_RGB32,W,H,&s2,1,at2,2);
    printf("   PRIME_2 (modifier-aware)          : %s\n", st==VA_STATUS_SUCCESS?"OK":vaErrorStr(st));
  }
  return 0;
}
