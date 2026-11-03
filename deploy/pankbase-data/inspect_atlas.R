library(Matrix)
args <- commandArgs(TRUE)
obj <- readRDS(args[1])
cat('class:',class(obj),'\n')
cat('slots:',names(attributes(obj)),'\n')
assays <- slot(obj,'assays')
cat('assays:',names(assays),'\n')
for(n in names(assays)) {
 a <- assays[[n]]
 cat('assay',n,'class',class(a),'slots',names(attributes(a)),'\n')
 for(k in c('counts','data','layers')) {
  if(k %in% names(attributes(a))) {
   m <- slot(a,k)
   cat(k,'class',class(m),'dimensions',dim(m),'\n')
   if(k=='layers') cat('layer names',names(m),'\n')
  }
 }
}
