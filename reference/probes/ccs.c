#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <gbm.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_drmcommon.h>
#include <va/va_str.h>
#include <stdlib.h>

static void try_ccs(VADisplay va, struct gbm_device *g, int use_lseek, const char* label){
  uint64_t mod=0x0100000000000004ull;
  struct gbm_bo *bo=gbm_bo_create_with_modifiers2(g,1920,1080,GBM_FORMAT_XRGB8888,&mod,1,GBM_BO_USE_RENDERING);
  if(!bo){printf("%s: alloc failed\n",label);return;}
  int np=gbm_bo_get_plane_count(bo);
  VADRMPRIMESurfaceDescriptor d; memset(&d,0,sizeof d);
  d.fourcc=VA_FOURCC_BGRX; d.width=1920; d.height=1080; d.num_objects=1;
  d.objects[0].fd=gbm_bo_get_fd_for_plane(bo,0);
  d.objects[0].size = use_lseek ? (uint32_t)lseek(d.objects[0].fd,0,SEEK_END)
                                : gbm_bo_get_stride_for_plane(bo,0)*1080;
  d.objects[0].drm_format_modifier=gbm_bo_get_modifier(bo);
  d.num_layers=1; d.layers[0].drm_format=GBM_FORMAT_XRGB8888; d.layers[0].num_planes=np;
  for(int p=0;p<np;p++){d.layers[0].object_index[p]=0;
    d.layers[0].offset[p]=gbm_bo_get_offset(bo,p);
    d.layers[0].pitch[p]=gbm_bo_get_stride_for_plane(bo,p);
    printf("   plane%d off=%u pitch=%u\n",p,gbm_bo_get_offset(bo,p),gbm_bo_get_stride_for_plane(bo,p));}
  printf("   obj size used = %u (lseek says %ld)\n", d.objects[0].size, (long)lseek(d.objects[0].fd,0,SEEK_END));
  VASurfaceAttrib a[2]={{VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
                        {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&d}}}};
  VASurfaceID s; VAStatus st=vaCreateSurfaces(va,VA_RT_FORMAT_RGB32,1920,1080,&s,1,a,2);
  printf("%s -> %s\n",label, st==VA_STATUS_SUCCESS?"IMPORT OK":vaErrorStr(st));
  if(st==VA_STATUS_SUCCESS)vaDestroySurfaces(va,&s,1);
  close(d.objects[0].fd); gbm_bo_destroy(bo);
}
static void try_fourcc(VADisplay va, struct gbm_device *g, uint32_t fcc, const char*label){
  uint64_t mod=0x0100000000000002ull;
  struct gbm_bo *bo=gbm_bo_create_with_modifiers2(g,1920,1080,GBM_FORMAT_XRGB8888,&mod,1,GBM_BO_USE_RENDERING);
  VADRMPRIMESurfaceDescriptor d; memset(&d,0,sizeof d);
  d.fourcc=fcc; d.width=1920;d.height=1080;d.num_objects=1;
  d.objects[0].fd=gbm_bo_get_fd_for_plane(bo,0);
  d.objects[0].size=(uint32_t)lseek(d.objects[0].fd,0,SEEK_END);
  d.objects[0].drm_format_modifier=gbm_bo_get_modifier(bo);
  d.num_layers=1;d.layers[0].drm_format=GBM_FORMAT_XRGB8888;d.layers[0].num_planes=1;
  d.layers[0].object_index[0]=0;d.layers[0].offset[0]=0;d.layers[0].pitch[0]=gbm_bo_get_stride_for_plane(bo,0);
  VASurfaceAttrib a[2]={{VASurfaceAttribMemoryType,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypeInteger,{.i=VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2}}},
                        {VASurfaceAttribExternalBufferDescriptor,VA_SURFACE_ATTRIB_SETTABLE,{VAGenericValueTypePointer,{.p=&d}}}};
  VASurfaceID s; VAStatus st=vaCreateSurfaces(va,VA_RT_FORMAT_RGB32,1920,1080,&s,1,a,2);
  printf("fourcc %s -> %s\n",label, st==VA_STATUS_SUCCESS?"IMPORT OK":vaErrorStr(st));
  if(st==VA_STATUS_SUCCESS)vaDestroySurfaces(va,&s,1);
  close(d.objects[0].fd);gbm_bo_destroy(bo);
}
int main(void){
  int fd=open("/dev/dri/renderD128",O_RDWR|O_CLOEXEC);
  struct gbm_device *g=gbm_create_device(fd);
  VADisplay va=vaGetDisplayDRM(fd);int mj,mn;
  if(vaInitialize(va,&mj,&mn)!=VA_STATUS_SUCCESS){puts("init fail");return 1;}
  try_ccs(va,g,0,"CCS stride*h size");
  try_ccs(va,g,1,"CCS lseek size");
  try_fourcc(va,g,VA_FOURCC_BGRX,"BGRX");
  try_fourcc(va,g,VA_FOURCC_RGBX,"RGBX");
  try_fourcc(va,g,VA_FOURCC_ARGB,"ARGB");
  return 0;
}
