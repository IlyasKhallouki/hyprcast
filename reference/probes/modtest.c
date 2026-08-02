#define _GNU_SOURCE
#include <stdio.h>
#include <fcntl.h>
#include <unistd.h>
#include <gbm.h>
static const uint64_t M[] = {0, 0x0100000000000001ull, 0x0100000000000002ull, 0x0100000000000004ull};
static const char *N[] = {"LINEAR","X_TILED","Y_TILED","Y_TILED_CCS"};
int main(void){
  int fd = open("/dev/dri/renderD128", O_RDWR|O_CLOEXEC);
  struct gbm_device *g = gbm_create_device(fd);
  for (int i=0;i<4;i++){
    struct gbm_bo *bo = gbm_bo_create_with_modifiers2(g,1920,1080,GBM_FORMAT_XRGB8888,&M[i],1,GBM_BO_USE_RENDERING);
    if(!bo){printf("%-12s ALLOC FAILED\n",N[i]);continue;}
    printf("%-12s planes=%d stride0=%u offset0=%u mod=0x%016lx size=%llu\n",N[i],
      gbm_bo_get_plane_count(bo), gbm_bo_get_stride_for_plane(bo,0), gbm_bo_get_offset(bo,0),
      (unsigned long)gbm_bo_get_modifier(bo), (unsigned long long)gbm_bo_get_stride(bo)*1080);
    gbm_bo_destroy(bo);
  }
  // what does plain create (no modifiers) give
  struct gbm_bo *bo = gbm_bo_create(g,1920,1080,GBM_FORMAT_XRGB8888,GBM_BO_USE_RENDERING);
  printf("%-12s planes=%d mod=0x%016lx\n","no-mod-list",gbm_bo_get_plane_count(bo),(unsigned long)gbm_bo_get_modifier(bo));
  return 0;
}
