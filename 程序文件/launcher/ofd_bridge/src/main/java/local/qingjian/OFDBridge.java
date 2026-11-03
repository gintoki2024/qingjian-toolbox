package local.qingjian;

import java.nio.file.Files;
import java.nio.file.Path;
import org.ofdrw.converter.export.PDFExporter;
import org.ofdrw.converter.export.TextExporter;
import org.ofdrw.converter.ofdconverter.PDFConverter;
import org.ofdrw.converter.ofdconverter.ImageConverter;
import org.ofdrw.converter.ofdconverter.TextConverter;

/** Small command-only bridge; all dependencies and Java are shipped offline. */
public final class OFDBridge {
    private OFDBridge() {}

    public static void main(String[] args) {
        try {
            if (args.length == 1 && "--version".equals(args[0])) {
                System.out.println("Qingjian OFD bridge 0.9.0 / OFDRW 2.5.1");
                return;
            }
            if (args.length != 3) throw new IllegalArgumentException("Expected: mode input output");
            String mode = args[0];
            Path input = Path.of(args[1]).toAbsolutePath().normalize();
            Path output = Path.of(args[2]).toAbsolutePath().normalize();
            if (!Files.isRegularFile(input) || Files.isSymbolicLink(input))
                throw new IllegalArgumentException("Input is not an ordinary file");
            if (Files.exists(output)) throw new IllegalArgumentException("Output already exists");
            if (!Files.isDirectory(output.getParent())) throw new IllegalArgumentException("Output folder is missing");
            // The Python caller checks ZIP/XML references before launching us.
            // Also disable Java XML's external DTD/schema access defensively.
            System.setProperty("javax.xml.accessExternalDTD", "");
            System.setProperty("javax.xml.accessExternalSchema", "");
            System.setProperty("javax.xml.accessExternalStylesheet", "");
            switch (mode) {
                case "ofd-pdf":
                    try (PDFExporter exporter = new PDFExporter(input, output)) { exporter.export(); }
                    break;
                case "ofd-txt":
                    try (TextExporter exporter = new TextExporter(input, output)) { exporter.export(); }
                    break;
                case "pdf-ofd":
                    try (PDFConverter converter = new PDFConverter(output)) {
                        converter.setEnableCopyAttachFiles(false);
                        converter.convert(input);
                    }
                    break;
                case "image-ofd":
                    try (ImageConverter converter = new ImageConverter(output)) { converter.convert(input); }
                    break;
                case "txt-ofd":
                    try (TextConverter converter = new TextConverter(output)) { converter.convert(input); }
                    break;
                default: throw new IllegalArgumentException("Unsupported conversion mode: " + mode);
            }
            if (!Files.isRegularFile(output)) throw new IllegalStateException("Converter did not write an output file");
            if (!mode.equals("ofd-txt") && Files.size(output) == 0) throw new IllegalStateException("Output is empty");
            System.out.println("QINGJIAN_OFD_OK");
        } catch (Throwable failure) {
            System.err.println(failure.getClass().getSimpleName() + ": " + failure.getMessage());
            Throwable cause = failure.getCause();
            if (cause != null) System.err.println(cause.getClass().getSimpleName() + ": " + cause.getMessage());
            System.exit(2);
        }
    }
}
