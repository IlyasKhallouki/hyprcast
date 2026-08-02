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

static const uint64_t M[] = {0x0100000000000002ull, 0x0100000000000004ull, 0, 0x0100000000000001ull};
static const char *N[] = {"Y_TILED","Y_TILED_CCS","LINEAR","X_TILED"};

static int try_import(VADisplay va, struct gbm_device *g, uint64_t mod, const char *name, uint32_t va_fourcc, uint32_t rt) {
  struct gbm_bo *bo = gbm_bo_create_with_modifiers2(g,1920,1080,GBM_FORMAT_XRGB8888,&mod,1,GBM_BO_USE_RENDERING);
  if(!bo){printf("  %-12s alloc failed\n",name);return -1;}
  int np = gbm_bo_get_plane_count(bo);
  VADRMPRIMESurfaceDescriptor d; memset(&d,0,sizeof d);
  d.fourcc = va_fourcc; d.width = 1920; d.height = 1080;
  d.num_objects = 1;
  d.objects[0].fd = gbm_bo_get_fd_for_plane(bo,0);
  d.objects[0].size = gbm_bo_get_stride_for_plane(bo,0)*1080;
  d.objects[0].drm_format_modifier = gbm_bo_get_modifier(bo);
  d.num_layers = 1;
  d.layers[0].drm_format = GBM_FORMAT_XRGB8888;
  d.layers[0].num_planes = np;
  for(int p=0;p<np;p++){ d.layers[0].object_index[p]=0;
    d.layers[0].offset[p]=gbm_bo_get_offset(bo,p);
    d.layers[0].pitch[p]=gbm_bo_get_stride_for_plane(bo,p); }
  VASurfaceAttrib attrs[2];
  attrs[0].type = VASurfaceAttribMemoryType; attrs[0].flags = VA_SURFACE_ATTRIB_SETTABLE;
  attrs[0].value.type = VAGenericValueTypeInteger;
  attrs[0].value.value.i = VA_SURFACE_ATTRIB_MEM_TYPE_DRM_PRIME_2;
  attrs[1].type = VASurfaceAttribExternalBufferDescriptor; attrs[1].flags = VA_SURFACE_ATTRIB_SETTABLE;
  attrs[1].value.type = VAGenericValueTypePointer; attrs[1].value.value.p = &d;
  VASurfaceID s;
  VAStatus st = vaCreateSurfaces(va, rt, 1920, 1080, &s, 1, attrs, 2);
  printf("  %-12s planes=%d fourcc=%.4s rt=0x%x -> %s\n", name, np, (char*)&va_fourcc, rt,
         st==VA_STATUS_SUCCESS?"IMPORT OK":vaErrorStr(st));
  if(st==VA_STATUS_SUCCESS) vaDestroySurfaces(va,&s,1);
  close(d.objects[0].fd);
  gbm_bo_destroy(bo);
  return st==VA_STATUS_SUCCESS?0:-1;
}
int main(void){
  int fd = open("/dev/dri/renderD128", O_RDWR|O_CLOEXEC);
  struct gbm_device *g = gbm_create_device(fd);
  VADisplay va = vaGetDisplayDRM(fd);
  int maj,min; VAStatus st = vaInitialize(va,&maj,&min);
  if(st!=VA_STATUS_SUCCESS){printf("vaInitialize failed: %s\n",vaErrorStr(st));return 1;}
  printf("VA-API %d.%d  driver: %s\n\n", maj,min, vaQueryVendorString(va));
  printf("Import XRGB8888 dmabuf as VA_FOURCC_BGRX / RT_FORMAT_RGB32:\n");
  for(int i=0;i<4;i++) try_import(va,g,M[i],N[i],VA_FOURCC_BGRX,VA_RT_FORMAT_RGB32);
  printf("\nEntrypoint/profile check for H264 encode:\n");
  int np = vaMaxNumProfiles(va); VAProfile *pr = calloc(np,sizeof *pr);
  vaQueryConfigProfiles(va,pr,&np);
  for(int i=0;i<np;i++){
    if(pr[i]!=VAProfileH264Main && pr[i]!=VAProfileH264ConstrainedBaseline && pr[i]!=VAProfileH264High) continue;
    int ne = vaMaxNumEntrypoints(va); VAEntrypoint *ep = calloc(ne,sizeof *ep);
    vaQueryConfigEntrypoints(va,pr[i],ep,&ne);
    printf("  %s:",vaProfileStr(pr[i]));
    for(int j=0;j<ne;j++) printf(" %s",vaEntrypointStr(ep[j]));
    printf("\n"); free(ep);
  }
  printf("\nVPP (VAEntrypointVideoProc) present: ");
  int ne = vaMaxNumEntrypoints(va); VAEntrypoint *ep = calloc(ne,sizeof *ep);
  if(vaQueryConfigEntrypoints(va,VAProfileNone,ep,&ne)==VA_STATUS_SUCCESS){
    int found=0; for(int j=0;j<ne;j++) if(ep[j]==VAEntrypointVideoProc) found=1;
    printf("%s\n", found?"YES":"no");
  } else printf("query failed\n");
  return 0;
}
