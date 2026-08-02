#include <stdio.h>
#include <fcntl.h>
#include <va/va.h>
#include <va/va_drm.h>
int main(){int fd=open("/dev/dri/renderD128",O_RDWR);VADisplay d=vaGetDisplayDRM(fd);int a,b;vaInitialize(d,&a,&b);
 VAProfile ps[3]={VAProfileH264ConstrainedBaseline,VAProfileH264Main,VAProfileH264High};
 const char*n[3]={"CBP","Main","High"};
 for(int i=0;i<3;i++){VAConfigAttrib at={VAConfigAttribRTFormat,VA_RT_FORMAT_RGB32};VAConfigID c;
  VAStatus s=vaCreateConfig(d,ps[i],VAEntrypointEncSlice,&at,1,&c);
  printf("  H264 %-5s EncSlice with RT_FORMAT_RGB32: %s\n",n[i],s==VA_STATUS_SUCCESS?"ACCEPTED":vaErrorStr(s));}
 return 0;}
