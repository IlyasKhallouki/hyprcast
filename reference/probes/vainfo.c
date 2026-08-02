#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <string.h>
#include <va/va.h>
#include <va/va_drm.h>
#include <va/va_str.h>

int main(int argc, char**argv){
  const char*dev = argc>1?argv[1]:"/dev/dri/renderD128";
  int fd = open(dev, O_RDWR);
  if(fd<0){perror("open");return 1;}
  VADisplay d = vaGetDisplayDRM(fd);
  int maj,min;
  VAStatus s = vaInitialize(d,&maj,&min);
  if(s!=VA_STATUS_SUCCESS){printf("vaInitialize failed: %s\n", vaErrorStr(s));return 1;}
  printf("VA-API version: %d.%d\n", maj,min);
  printf("Driver: %s\n", vaQueryVendorString(d));
  int maxp = vaMaxNumProfiles(d);
  VAProfile *profs = calloc(maxp,sizeof(VAProfile));
  int np=0;
  vaQueryConfigProfiles(d,profs,&np);
  int maxe = vaMaxNumEntrypoints(d);
  VAEntrypoint *eps = calloc(maxe,sizeof(VAEntrypoint));
  for(int i=0;i<np;i++){
    int ne=0;
    if(vaQueryConfigEntrypoints(d,profs[i],eps,&ne)!=VA_STATUS_SUCCESS) continue;
    for(int j=0;j<ne;j++){
      printf("%-42s : %s\n", vaProfileStr(profs[i]), vaEntrypointStr(eps[j]));
    }
  }
  printf("\n=== H264 ENCODE CONFIG ATTRIBUTES ===\n");
  VAProfile h264[] = {VAProfileH264ConstrainedBaseline, VAProfileH264Main, VAProfileH264High};
  const char* names[]={"ConstrainedBaseline","Main","High"};
  VAEntrypoint entries[] = {VAEntrypointEncSlice, VAEntrypointEncSliceLP};
  const char* enames[]={"EncSlice","EncSliceLP"};
  for(int i=0;i<3;i++) for(int e=0;e<2;e++){
    VAConfigAttrib attrs[] = {
      {VAConfigAttribRateControl,0},{VAConfigAttribEncPackedHeaders,0},
      {VAConfigAttribEncMaxRefFrames,0},{VAConfigAttribEncMaxSlices,0},
      {VAConfigAttribEncSliceStructure,0},{VAConfigAttribEncQualityRange,0},
      {VAConfigAttribEncIntraRefresh,0},{VAConfigAttribEncROI,0},
      {VAConfigAttribRTFormat,0},{VAConfigAttribMaxPictureWidth,0},
      {VAConfigAttribMaxPictureHeight,0},{VAConfigAttribEncMacroblockInfo,0},
      {VAConfigAttribEncSkipFrame,0},{VAConfigAttribEncTileSupport,0},
    };
    int n = sizeof(attrs)/sizeof(attrs[0]);
    VAStatus st = vaGetConfigAttributes(d,h264[i],entries[e],attrs,n);
    if(st!=VA_STATUS_SUCCESS){ printf("\n-- H264 %s / %s : UNSUPPORTED (%s)\n", names[i],enames[e],vaErrorStr(st)); continue;}
    printf("\n-- H264 %s / %s : SUPPORTED\n", names[i],enames[e]);
    for(int k=0;k<n;k++){
      if(attrs[k].value==VA_ATTRIB_NOT_SUPPORTED){ printf("   %-32s: not supported\n", vaConfigAttribTypeStr(attrs[k].type)); continue;}
      printf("   %-32s: 0x%08x", vaConfigAttribTypeStr(attrs[k].type), attrs[k].value);
      if(attrs[k].type==VAConfigAttribRateControl){
        unsigned v=attrs[k].value; printf("  [");
        if(v&VA_RC_CQP)printf("CQP ");if(v&VA_RC_CBR)printf("CBR ");if(v&VA_RC_VBR)printf("VBR ");
        if(v&VA_RC_VCM)printf("VCM ");if(v&VA_RC_CFS)printf("CFS ");if(v&VA_RC_ICQ)printf("ICQ ");
        if(v&VA_RC_MB)printf("MB ");if(v&VA_RC_QVBR)printf("QVBR ");if(v&VA_RC_AVBR)printf("AVBR ");
        if(v&VA_RC_TCBRC)printf("TCBRC ");printf("]");
      }
      if(attrs[k].type==VAConfigAttribEncMaxRefFrames){
        printf("  [L0=%u L1=%u]", attrs[k].value&0xffff, (attrs[k].value>>16)&0xffff);
      }
      if(attrs[k].type==VAConfigAttribRTFormat){
        unsigned v=attrs[k].value; printf("  [");
        if(v&VA_RT_FORMAT_YUV420)printf("YUV420 ");if(v&VA_RT_FORMAT_YUV422)printf("YUV422 ");
        if(v&VA_RT_FORMAT_YUV444)printf("YUV444 ");if(v&VA_RT_FORMAT_RGB32)printf("RGB32 ");
        if(v&VA_RT_FORMAT_YUV420_10)printf("YUV420_10 ");printf("]");
      }
      printf("\n");
    }
  }
  printf("\n=== VPP (VideoProc) ===\n");
  {
    VAConfigAttrib a[]={{VAConfigAttribRTFormat,0}};
    VAStatus st=vaGetConfigAttributes(d,VAProfileNone,VAEntrypointVideoProc,a,1);
    if(st==VA_STATUS_SUCCESS){
      unsigned v=a[0].value;
      printf("VPP RTFormat: 0x%x [",v);
      if(v&VA_RT_FORMAT_YUV420)printf("YUV420 ");if(v&VA_RT_FORMAT_YUV422)printf("YUV422 ");
      if(v&VA_RT_FORMAT_YUV444)printf("YUV444 ");if(v&VA_RT_FORMAT_RGB32)printf("RGB32 ");
      printf("]\n");
      VAConfigID cfg;
      if(vaCreateConfig(d,VAProfileNone,VAEntrypointVideoProc,NULL,0,&cfg)==VA_STATUS_SUCCESS){
        VAContextID ctx;
        if(vaCreateContext(d,cfg,1920,1080,0,NULL,0,&ctx)==VA_STATUS_SUCCESS){
          unsigned nf=0;
          vaQueryVideoProcPipelineCaps(d,ctx,NULL,0,NULL);
          VAProcFilterType filters[VAProcFilterCount]; nf=VAProcFilterCount;
          if(vaQueryVideoProcFilters(d,ctx,filters,&nf)==VA_STATUS_SUCCESS){
            printf("VPP filters: %u\n",nf);
            for(unsigned i=0;i<nf;i++) printf("   filter type %d\n",filters[i]);
          }
          VAProcPipelineCaps caps; memset(&caps,0,sizeof(caps));
          if(vaQueryVideoProcPipelineCaps(d,ctx,NULL,0,&caps)==VA_STATUS_SUCCESS){
            printf("VPP pipeline: num_input_color_standards=%u num_output_color_standards=%u rotation=0x%x mirror=0x%x num_forward_ref=%u\n",
              caps.num_input_color_standards,caps.num_output_color_standards,caps.rotation_flags,caps.mirror_flags,caps.num_forward_references);
            printf("VPP input pixel formats (%u):",caps.num_input_pixel_formats);
            for(unsigned i=0;i<caps.num_input_pixel_formats && caps.input_pixel_format;i++){
              unsigned f=caps.input_pixel_format[i];
              printf(" %c%c%c%c",f&0xff,(f>>8)&0xff,(f>>16)&0xff,(f>>24)&0xff);
            }
            printf("\nVPP output pixel formats (%u):",caps.num_output_pixel_formats);
            for(unsigned i=0;i<caps.num_output_pixel_formats && caps.output_pixel_format;i++){
              unsigned f=caps.output_pixel_format[i];
              printf(" %c%c%c%c",f&0xff,(f>>8)&0xff,(f>>16)&0xff,(f>>24)&0xff);
            }
            printf("\n");
          }
          vaDestroyContext(d,ctx);
        }
        vaDestroyConfig(d,cfg);
      }
    } else printf("VPP unsupported: %s\n", vaErrorStr(st));
  }
  printf("\n=== SURFACE IMAGE FORMATS ===\n");
  {
    int m=vaMaxNumImageFormats(d); VAImageFormat*f=calloc(m,sizeof(VAImageFormat)); int n=0;
    vaQueryImageFormats(d,f,&n);
    for(int i=0;i<n;i++) printf(" %c%c%c%c", f[i].fourcc&0xff,(f[i].fourcc>>8)&0xff,(f[i].fourcc>>16)&0xff,(f[i].fourcc>>24)&0xff);
    printf("\n");
  }
  vaTerminate(d);
  return 0;
}
